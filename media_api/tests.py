import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from .auth import CurrentUser, _user_cache, cache_current_user, cached_current_user, require_user
from .views import bulk_public_query, cleanup_stale_tv_sessions, is_public_karaoke_media, karaoke_search_terms, media_search_query, public_karaoke_requested, remote_reserver_label, remote_session_role, search_tokens
from .youtube import check_yt_dlp, is_browser_playable_mp4, youtube_format_selector


class MediaSearchQueryTests(SimpleTestCase):
    def test_search_tokens_keep_numeric_and_text_terms(self):
        self.assertEqual(search_tokens("9900001 Always Awake"), ["9900001", "Always", "Awake"])

    def test_karaoke_number_terms_include_padded_and_ky_forms(self):
        terms = karaoke_search_terms("123")

        self.assertIn("123", terms)
        self.assertIn("0123", terms)
        self.assertIn("KY.0123", terms)
        self.assertIn("KY0123", terms)

    def test_karaoke_search_query_combines_phrase_and_token_fallback(self):
        query = media_search_query(
            ["title", "display_name", "file_name", "tags", "karaoke_number", "karaoke_artist"],
            "9900001 Always",
            True,
        )

        self.assertIn("$or", query)
        self.assertIn({"$and": [
            {"$or": [
                {"title": {"$regex": "9900001", "$options": "i"}},
                {"display_name": {"$regex": "9900001", "$options": "i"}},
                {"file_name": {"$regex": "9900001", "$options": "i"}},
                {"tags": {"$regex": "9900001", "$options": "i"}},
                {"karaoke_number": {"$regex": "9900001", "$options": "i"}},
                {"karaoke_artist": {"$regex": "9900001", "$options": "i"}},
            ]},
            {"$or": [
                {"title": {"$regex": "Always", "$options": "i"}},
                {"display_name": {"$regex": "Always", "$options": "i"}},
                {"file_name": {"$regex": "Always", "$options": "i"}},
                {"tags": {"$regex": "Always", "$options": "i"}},
                {"karaoke_number": {"$regex": "Always", "$options": "i"}},
                {"karaoke_artist": {"$regex": "Always", "$options": "i"}},
            ]},
        ]}, query["$or"])

    def test_general_media_search_query_escapes_regex_keyword(self):
        query = media_search_query(["title", "display_name"], "a+b [live]", False)

        self.assertIn({"title": {"$regex": "a\\+b\\ \\[live\\]", "$options": "i"}}, query["$or"])
        self.assertIn({"$and": [
            {"$or": [
                {"title": {"$regex": "live", "$options": "i"}},
                {"display_name": {"$regex": "live", "$options": "i"}},
            ]},
        ]}, query["$or"])

    def test_bulk_public_query_targets_all_videos_by_default(self):
        self.assertEqual(bulk_public_query({}), {"content_kind": "VIDEO"})

    def test_bulk_public_query_can_target_karaoke_videos(self):
        self.assertEqual(bulk_public_query({"karaoke_only": True}), {"content_kind": "VIDEO", "tags": "노래방"})

    def test_bulk_public_query_treats_false_string_as_false(self):
        self.assertEqual(bulk_public_query({"karaoke_only": "false"}), {"content_kind": "VIDEO"})

    def test_karaoke_public_access_is_default_for_song_list(self):
        self.assertTrue(public_karaoke_requested("KARAOKE"))
        self.assertTrue(public_karaoke_requested("karaoke", "true"))
        self.assertFalse(public_karaoke_requested("KARAOKE", "false"))
        self.assertFalse(public_karaoke_requested("VIDEO"))

    def test_public_karaoke_media_requires_admin_public_flag_and_karaoke_tag(self):
        self.assertTrue(is_public_karaoke_media({"owner_is_admin": True, "content_kind": "VIDEO", "tags": ["노래방"]}))
        self.assertFalse(is_public_karaoke_media({"owner_is_admin": False, "content_kind": "VIDEO", "tags": ["노래방"]}))
        self.assertFalse(is_public_karaoke_media({"owner_is_admin": True, "content_kind": "VIDEO", "tags": ["youtube"]}))


class KaraokeRemoteRoleTests(SimpleTestCase):
    def test_only_session_owner_is_host_even_when_invited_user_is_admin(self):
        session = {
            "session_type": "TV",
            "owner_user_id": "first-user",
            "participants": [{"user_id": "invited-admin"}],
        }
        invited_admin = CurrentUser(user_id="invited-admin", roles=["ROLE_ADMIN"], service_permissions={})

        self.assertEqual(remote_session_role(session, invited_admin), "GUEST")

    def test_first_paired_user_is_host(self):
        session = {"session_type": "TV", "owner_user_id": "first-user", "participants": []}
        owner = CurrentUser(user_id="first-user", roles=[], service_permissions={})

        self.assertEqual(remote_session_role(session, owner), "HOST")

    def test_reservation_label_uses_login_id(self):
        user = CurrentUser(user_id="admin-user", roles=["ROLE_ADMIN"], service_permissions={})

        self.assertEqual(remote_reserver_label("HOST", user), "admin-user")

    def test_cleanup_expires_stale_tv_sessions_and_clears_old_queue(self):
        remote_collection = MagicMock()
        queue_collection = MagicMock()
        remote_collection.update_many.return_value.modified_count = 2
        queue_collection.update_many.return_value.modified_count = 1
        remote_collection.find.return_value.limit.return_value = [
            {"owner_user_id": "owner-a"},
            {"owner_user_id": "owner-b"},
        ]

        with patch("media_api.views.karaoke_remote_collection", return_value=remote_collection), \
            patch("media_api.views.karaoke_queue_collection", return_value=queue_collection), \
            patch("media_api.views._TV_LAST_CLEANUP_MONOTONIC", 0.0):
            result = cleanup_stale_tv_sessions(force=True)

        self.assertEqual(result, {"expired_count": 2, "queue_clear_count": 1})
        stale_query = remote_collection.update_many.call_args[0][0]
        self.assertEqual(stale_query["session_type"], "TV")
        self.assertEqual(stale_query["status"], "PAIRED")
        queue_query = queue_collection.update_many.call_args[0][0]
        self.assertEqual(queue_query["owner_user_id"]["$in"], ["owner-a", "owner-b"])


class AuthCacheTests(SimpleTestCase):
    def tearDown(self):
        _user_cache.clear()

    def test_webhard_permissions_grant_media_access(self):
        user = CurrentUser(
            user_id="sihyeon",
            roles=[],
            service_permissions={"WEBHARD_SERVICE": ["WRITE", "DELETE", "SHARE"]},
        )

        self.assertTrue(user.has_any_media_permission())
        self.assertTrue(user.has_permission("WRITE"))
        self.assertTrue(user.has_permission("DELETE"))
        self.assertFalse(user.has_permission("ADMIN"))

    def test_disabled_media_service_returns_service_disabled_code(self):
        request = RequestFactory().get("/api/me/", HTTP_AUTHORIZATION="Bearer token")
        user = CurrentUser(user_id="sihyeon", roles=[], service_permissions={"WEBHARD_SERVICE": ["READ"]})

        with patch("media_api.auth.fetch_current_user", return_value=user), \
            patch("media_api.auth.fetch_service_status", return_value={"use_yn": "N"}):
            response = require_user(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)["code"], "SERVICE_DISABLED")

    def test_user_cache_does_not_store_raw_access_token(self):
        token = "raw.jwt.token"
        user = CurrentUser(user_id="admin-user", roles=["ROLE_ADMIN"], service_permissions={}, access_token=token)

        cache_current_user(token, user)

        self.assertNotIn(token, _user_cache)
        cached_values = [cached_user for _expires_at, cached_user in _user_cache.values()]
        self.assertTrue(cached_values)
        self.assertEqual(cached_values[0].access_token, "")
        self.assertEqual(cached_current_user(token).access_token, token)

    @override_settings(MEDIA_CONFIG={"AUTH_CACHE_SECONDS": 5, "AUTH_CACHE_MAX_ENTRIES": 2})
    def test_user_cache_prunes_oldest_entries(self):
        for index in range(3):
            cache_current_user(f"token-{index}", CurrentUser(user_id=f"user-{index}", roles=[], service_permissions={}))

        self.assertLessEqual(len(_user_cache), 2)
        self.assertIsNone(cached_current_user("token-0"))
        self.assertEqual(cached_current_user("token-1").user_id, "user-1")
        self.assertEqual(cached_current_user("token-2").user_id, "user-2")


class YoutubeToolTests(SimpleTestCase):
    def test_check_yt_dlp_auto_updates_old_version(self):
        with patch("media_api.youtube.yt_dlp_command", return_value="yt-dlp"), \
            patch("media_api.youtube.latest_yt_dlp_version", return_value="2026.07.07"), \
            patch("media_api.youtube.command_output", side_effect=["2026.01.01", "2026.07.07"]), \
            patch("media_api.youtube.update_yt_dlp", return_value={"message": "updated"}) as update:
            result = check_yt_dlp(auto_update=True)

        update.assert_called_once_with("yt-dlp")
        self.assertTrue(result["is_latest"])
        self.assertEqual(result["version"], "2026.07.07")

    def test_browser_playable_mp4_requires_aac_audio(self):
        self.assertTrue(is_browser_playable_mp4({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }))
        self.assertFalse(is_browser_playable_mp4({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "opus"},
            ],
        }))

    def test_browser_playable_mp4_requires_h264_yuv420p(self):
        self.assertFalse(is_browser_playable_mp4({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {"codec_type": "video", "codec_name": "av1", "pix_fmt": "yuv420p"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }))
        self.assertFalse(is_browser_playable_mp4({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv444p"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }))

    @override_settings(MEDIA_CONFIG={"YOUTUBE_VIDEO_MAX_HEIGHT": 720})
    def test_youtube_format_selector_prefers_limited_h264_mp4(self):
        selector = youtube_format_selector()

        self.assertIn("[height<=720]", selector)
        self.assertIn("[vcodec^=avc1]", selector)
        self.assertIn("ba[ext=m4a]", selector)

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from django.conf import settings


_client: MongoClient | None = None
_media_collection: Collection | None = None
_karaoke_remote_collection: Collection | None = None
_karaoke_queue_collection: Collection | None = None
_karaoke_pair_attempt_collection: Collection | None = None
_media_user_state_collection: Collection | None = None


def mongo_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(settings.MEDIA_CONFIG["MEDIA_MONGO_URI"], serverSelectionTimeoutMS=5000)
    return _client


def media_collection():
    global _media_collection
    if _media_collection is not None:
        return _media_collection
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["media_items"]
    collection.create_index([("webhard_file_id", ASCENDING)], unique=True)
    collection.create_index([("owner_user_id", ASCENDING), ("original_created_at", DESCENDING), ("webhard_file_id", DESCENDING)])
    collection.create_index([("owner_user_id", ASCENDING), ("content_kind", ASCENDING), ("original_created_at", DESCENDING)])
    collection.create_index([("owner_user_id", ASCENDING), ("tags", ASCENDING)])
    collection.create_index([("owner_user_id", ASCENDING), ("album", ASCENDING)])
    collection.create_index([("owner_is_admin", ASCENDING), ("original_created_at", DESCENDING), ("webhard_file_id", DESCENDING)])
    collection.create_index([("content_kind", ASCENDING), ("original_created_at", DESCENDING), ("webhard_file_id", DESCENDING)])
    collection.create_index([("tags", ASCENDING), ("original_created_at", DESCENDING), ("webhard_file_id", DESCENDING)])
    _media_collection = collection
    return _media_collection


def karaoke_remote_collection():
    global _karaoke_remote_collection
    if _karaoke_remote_collection is not None:
        return _karaoke_remote_collection
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["karaoke_remote_sessions"]
    collection.create_index([("session_id", ASCENDING)], unique=True)
    collection.create_index([("session_type", ASCENDING), ("tv_token_hash", ASCENDING)])
    collection.create_index([("session_type", ASCENDING), ("pairing_token_hash", ASCENDING)])
    collection.create_index([("session_type", ASCENDING), ("pairing_code", ASCENDING), ("status", ASCENDING)])
    collection.create_index([("owner_user_id", ASCENDING), ("updated_at", DESCENDING)])
    collection.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    _karaoke_remote_collection = collection
    return _karaoke_remote_collection


def karaoke_queue_collection():
    global _karaoke_queue_collection
    if _karaoke_queue_collection is not None:
        return _karaoke_queue_collection
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["karaoke_account_queues"]
    collection.create_index([("owner_user_id", ASCENDING)], unique=True)
    collection.create_index([("updated_at", DESCENDING)])
    _karaoke_queue_collection = collection
    return _karaoke_queue_collection


def karaoke_pair_attempt_collection():
    global _karaoke_pair_attempt_collection
    if _karaoke_pair_attempt_collection is not None:
        return _karaoke_pair_attempt_collection
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["karaoke_pair_attempts"]
    collection.create_index([("key", ASCENDING), ("created_at", DESCENDING)])
    collection.create_index([("created_at", ASCENDING)], expireAfterSeconds=600)
    _karaoke_pair_attempt_collection = collection
    return _karaoke_pair_attempt_collection


def media_user_state_collection():
    global _media_user_state_collection
    if _media_user_state_collection is not None:
        return _media_user_state_collection
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["media_user_states"]
    collection.create_index([("user_id", ASCENDING), ("webhard_file_id", ASCENDING)], unique=True)
    collection.create_index([("user_id", ASCENDING), ("favorite", ASCENDING), ("updated_at", DESCENDING)])
    collection.create_index([("webhard_file_id", ASCENDING), ("liked", ASCENDING)])
    _media_user_state_collection = collection
    return _media_user_state_collection

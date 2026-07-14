from django.urls import include, path


urlpatterns = [
    path("api/", include("media_api.urls")),
]


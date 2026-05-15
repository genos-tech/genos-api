from django.urls import path

from origin.search_engine.views import SearchView

urlpatterns = [
    path("api/v2/search/", SearchView.as_view(), name="search"),
]

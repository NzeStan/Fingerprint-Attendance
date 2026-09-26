from __future__ import annotations

from rest_framework.pagination import CursorPagination, PageNumberPagination

from ..conf import settings


class StandardPagination(PageNumberPagination):
    page_size_query_param = "page_size"
    max_page_size = 1000

    def get_page_size(self, request):  # type: ignore[no-untyped-def]
        self.page_size = settings.API_PAGE_SIZE
        return super().get_page_size(request)


class PunchCursorPagination(CursorPagination):
    """Stable, efficient pagination for large punch tables."""

    ordering = ("-punched_at", "-id")
    page_size_query_param = "page_size"
    max_page_size = 1000

    def get_page_size(self, request):  # type: ignore[no-untyped-def]
        self.page_size = settings.API_PAGE_SIZE
        return super().get_page_size(request)

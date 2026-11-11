from .models import LibrarySetting


def library(request):
    return {"library": LibrarySetting.load()}

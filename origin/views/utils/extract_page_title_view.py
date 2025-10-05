# views.py
import re
import requests
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
@require_GET
def get_page_title(request):
    url = request.GET.get("url")
    if not url:
        return JsonResponse({"error": "Missing 'url' parameter"}, status=400)

    try:
        # Fetch page
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        html = response.text

        # Extract <title>...</title>
        match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = match.group(1).strip() if match else None

        return JsonResponse({"url": url, "title": title})
    except Exception as e:
        print(f"ERROR in get_page_title: {e}")
        return JsonResponse({"url": url, "title": url})

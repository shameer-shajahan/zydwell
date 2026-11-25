"""The chat page and the endpoint its messages go to."""
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.constants import MODULE_GUESTS
from core.permissions import require_module

from . import dialog


@require_module(MODULE_GUESTS)
def chat(request):
    return render(request, 'assistant/chat.html', {'opening': dialog.opening(request)})


@require_module(MODULE_GUESTS)
@require_POST
def message(request):
    return JsonResponse({'replies': dialog.reply(request, request.POST.get('text', ''))})

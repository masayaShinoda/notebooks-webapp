from django.conf import settings
from django.contrib.auth import login
from django.shortcuts import redirect, render

from .forms import RegistrationForm


def register(request):
    if not settings.ACCOUNT_SIGNUP_ENABLED:
        return render(request, 'accounts/signup_disabled.html', status=403)

    if request.user.is_authenticated:
        return redirect('notebooks:workspace')

    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('notebooks:workspace')
    else:
        form = RegistrationForm()

    return render(request, 'accounts/register.html', {'form': form})

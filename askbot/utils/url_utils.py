import urlparse
from django.core.urlresolvers import reverse
from django.conf import settings

def strip_path(url):
    """srips path, params and hash fragments of the url"""
    purl = urlparse.urlparse(url)
    return urlparse.urlunparse(
        urlparse.ParseResult(
            purl.scheme,
            purl.netloc,
            '', '', '', ''
        )
    )

def get_login_url():
    return settings.LOGIN_URL

def get_logout_url():
    return settings.LOGOUT_URL

def get_logout_redirect_url():
    """returns internal logout redirect url,
    or settings.LOGOUT_REDIRECT_URL if it exists
    or url to the main page"""
    if hasattr(settings, 'LOGOUT_REDIRECT_URL'):
        return settings.LOGOUT_REDIRECT_URL
    else:
        return reverse('forum-index')

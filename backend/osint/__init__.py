# backend/osint/__init__.py
def run_osint(*args, **kwargs):
    from .automation import run_osint as _run
    return _run(*args, **kwargs)
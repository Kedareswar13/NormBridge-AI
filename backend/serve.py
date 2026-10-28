"""Production entry point: serves the Flask app with waitress (pure Python, works on Windows and Linux).

    python serve.py            # instead of `python app.py` (the Flask dev server)
"""
import os

from waitress import serve

from app import app
from config import PORT

if __name__ == '__main__':
    threads = int(os.environ.get('WAITRESS_THREADS', '8'))
    print(f"🚀 NormBridge AI API (waitress, {threads} threads) on http://localhost:{PORT}")
    # Windows only: browsers resolve "localhost" to ::1 first, and a refused IPv6 connection costs every request a
    # ~2 s retry against an IPv4-only server. (On Linux a refused connection fails instantly.)
    if os.name == 'nt':
        try:
            serve(app, listen=f"0.0.0.0:{PORT} [::]:{PORT}", threads=threads, channel_timeout=600)
            raise SystemExit(0)
        except OSError as e:
            print(f"⚠️ IPv6 listen failed ({e}); serving on IPv4 only")
    serve(app, host='0.0.0.0', port=PORT, threads=threads, channel_timeout=600)

"""Website entry point (Hugging Face Space). Same app as the laptop version, in CLOUD mode."""
import os
import sys
import threading

import engine
import server

if not server.load_settings()["pin"]:
    sys.exit("Set the APP_PIN secret (digits only) in the Space settings.")

threading.Thread(target=engine.cleanup_loop, daemon=True).start()
httpd = server.make_server(retries=1)
print(f"Link Downloader is live on port {server.PORT}", flush=True)
httpd.serve_forever()

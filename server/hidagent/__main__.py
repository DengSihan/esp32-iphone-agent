import logging
import os

import uvicorn

debug = os.environ.get("HIDAGENT_DEBUG") == "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("hidagent").setLevel(logging.DEBUG if debug else logging.INFO)
# the page controls the phone (and shows the board's hotspot password): this Mac only, unless HOST says otherwise
uvicorn.run("hidagent.app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8890)))

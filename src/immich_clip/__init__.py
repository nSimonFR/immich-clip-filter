"""immich-clip-filter — content-based auto-filing for Immich.

Two halves, because one of them cannot do the job alone:

* **the plugin** (``plugin/``) is a WASM workflow step running inside Immich. Its
  only way out is the ``httpRequest`` host function, which returns
  ``body: await res.text()`` — no image bytes in, no multipart out. So it cannot
  call CLIP itself;
* **the sidecar** (``immich_clip.sidecar``) answers the question instead, from the
  embeddings Immich has ALREADY computed and written to ``smart_search``. No
  second inference pass, no decoding originals.

Nothing in this package reads the environment at import time. Every entry point
builds a frozen ``Settings`` in ``main()`` and threads it down, and every I/O call
takes an injectable seam (``connect=``, ``opener=``, ``log=``, ``sleep=``) — which
is why the unit suite needs neither a database nor a network.
"""

__version__ = "1.0.0"

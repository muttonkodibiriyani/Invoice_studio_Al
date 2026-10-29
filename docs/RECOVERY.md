# Source recovery (2026-10-20)

The code in this repository was recovered from the running production deployment on 20 Oct 2026,
because no source copy existed anywhere else.

- Backend: extracted from the live Cloud Run image
  `us-central1-docker.pkg.dev/productintelligence-beeb3/inv-studio/app@sha256:c91d782e4d68429c280d82c239316aaaf09fb64227c62037da7b8a8cbbc8f348`
  (Artifact Registry tag `del-b1fb54f`; linux/amd64 manifest `sha256:fdff1c4b88e5b890fbaf37fcca86d36570cc83c722b9fae3d939ffe75f17f30a`),
  serving as revision `inv-studio-api-00099-7n2`. The tree under `/opt/inv-studio` is committed as-is
  (minus `build/`, `inv_studio.egg-info/` and `__pycache__`, which the build regenerates).
  `docs/live-image-history.jsonl` is the full build history (92 steps, 72 of them layers), `docs/live-image-pip-freeze.txt` and
  `docs/live-image-dpkg.txt` the installed packages. `Dockerfile` is a clean single build reconstructed from that history.
- Frontend: `hosting/public` holds the 11 files of Firebase Hosting version `8e2243be6414c5f4`
  (release `1791472197965000`, 8 Oct 2026 15:09:57 UTC) downloaded from the live site; `hosting/firebase.json`
  carries the headers and rewrites of that version (`docs/hosting-version-*.json`).
  Its two Firebase init files (`__/firebase/init.js` and `init.json`) were removed on 28 Oct 2026: nothing in the app
  loads them (sign-in reads `/api/public-config`), and they carry the project's web API key.
  Note that `hosting/public/static/app.js` is slightly newer than `app/static/app.js` inside the image
  (adds a "server is starting" retry loop); the Hosting copy is what users run.
- Tests: the live image contains no `tests/` directory; the original suite was not recoverable.

## Rebuild verification (20 Oct 2026)

`docker build --platform linux/amd64 .` from this repository was compared with the live image:

- `/opt/inv-studio`: byte-identical. Sorted sha256 over every file (excluding `*.pyc`) is
  `38351d758516d406d964a69ea3dec009308fc2d386cdf792003f7e6716b91ea8` for both the live tree and the rebuilt tree,
  and `diff -r` reports no differences (including the regenerated `build/` and `inv_studio.egg-info/`).
- `pip freeze`: identical package set and versions (165 pinned packages) once `constraints.txt` pins the transitive
  dependencies. The first rebuild without it drifted on 30 transitive packages, and `rapidocr` 3.10.0 made the
  docling warm-up fail (`PP_OCRV6_LANGS` missing); with the constraints the warm-up completes for invoice2data,
  paddleocr and docling exactly as in the live build.
- Layout: 20 layers, 5.64 GB (the live image has 72 layers from incremental `COPY app` builds).
- `firebase-auth.bundle.js` is built from `app/static/firebase-auth-entry.js` with esbuild 0.25.3,
  `@firebase/app` 0.16.2 and `@firebase/auth` 1.13.6 (Firebase JS SDK 12.19.0, the versions embedded in the live
  bundle). `scripts/firebase-bundle/` holds the exact versions (`package.json`), the lockfile with every package's
  integrity hash (`package-lock.json`) and the bundle's sha256 (`bundle.sha256`). On 28 Oct 2026 that recipe rebuilt
  the committed bundle byte for byte, and `tests/test_static_sync.py` fails if the bundle stops matching its
  checksum. To update the SDK: change the versions in `package.json`; in `scripts/firebase-bundle` run
  `npm install && npm run build && (cd ../../app/static && sha256sum firebase-auth.bundle.js) > bundle.sha256`;
  run `python3 scripts/sync_static.py` from the repo root; commit the bundle, its hosting copy and the three files.

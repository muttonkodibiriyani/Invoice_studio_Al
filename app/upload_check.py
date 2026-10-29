"""Checks an uploaded PDF or image (page count, decode and verify) in a short-lived, limited child process,
so a pdfium or Pillow bug in that first parse never runs inside the API process (security review M4). The
child answers with one key; the API maps it to its own message. The multi-invoice check (analyse_pdf, pdfplumber)
runs in a reader child too (engines.split_check). Still in the API process, deferred in the security review:
split_pdf (pdfium) on multi-invoice PDFs, and AI page rendering."""
import io
import subprocess
import sys

from .sandbox import limit

CPU_SECONDS=10
# Measured VmPeak: 46 MB for a 15 MB 20-page PDF, 130 MB for a 75 MB 25-megapixel TIFF. About 4x that:
# a parser that needs more is not reading an invoice.
MAX_AS_MB=512
TIMEOUT_SECONDS=20
MESSAGES={"pdf":"PDF is damaged or encrypted","pages":"Use invoices with 1 to 20 pages",
          "image":"Image is invalid or exceeds 25 megapixels / 20 frames"}


def check(content,suffix,env):
    fallback="pdf" if suffix==".pdf" else "image"
    try:
        proc=subprocess.run([sys.executable,"-m","app.upload_check",suffix],input=content,capture_output=True,
                            timeout=TIMEOUT_SECONDS,env=env)
    except subprocess.TimeoutExpired:
        raise ValueError(MESSAGES[fallback]) from None
    answer=proc.stdout.decode("ascii","replace").strip()
    if proc.returncode==0 and answer=="ok":return
    raise ValueError(MESSAGES.get(answer,MESSAGES[fallback]))


def inspect(content,suffix):
    if suffix==".pdf":
        import pypdfium2 as pdfium
        try:
            pdf=pdfium.PdfDocument(content);count=len(pdf);pdf.close()
        except Exception:return "pdf"
        return "ok" if 1<=count<=20 else "pages"
    from PIL import Image
    from .models import IMAGE_FORMATS
    try:
        with Image.open(io.BytesIO(content),formats=IMAGE_FORMATS) as image:
            if image.width*image.height>25_000_000 or getattr(image,"n_frames",1)>20:return "image"
            image.verify()
    except Exception:return "image"
    return "ok"


if __name__=="__main__":
    limit(CPU_SECONDS,MAX_AS_MB)
    print(inspect(sys.stdin.buffer.read(),sys.argv[1]))

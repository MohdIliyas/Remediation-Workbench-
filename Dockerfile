# Everything the tool needs in one image: OCR, LibreOffice, PDF tools.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa \
      ghostscript qpdf poppler-utils unpaper pngquant \
      libreoffice-writer-nogui libreoffice-impress-nogui fonts-dejavu fonts-liberation fonts-crosextra-carlito \
      libgl1 libglib2.0-0 \
    && (apt-get install -y --no-install-recommends libreoffice-math-nogui \
        || apt-get install -y --no-install-recommends libreoffice-math) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOST=0.0.0.0 PORT=8080 DATA_DIR=/data WORKERS=2
VOLUME /data
EXPOSE 8080
CMD ["python", "app.py"]

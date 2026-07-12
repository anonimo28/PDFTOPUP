#!/bin/bash
# Start the PDF → EPUB converter app
cd ~/pdf-to-epub
source venv/bin/activate
PYTHONPATH="" python app.py "$@"

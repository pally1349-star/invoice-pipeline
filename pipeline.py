import os
import json
import base64
import tempfile
import time
import fitz
import gspread
from groq import Groq
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─────────────────────────────────────────────
# CONFIGURATION — fill these in
# ─────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
CREDENTIALS_FILE  = "credentials.json"
SPREADSHEET_NAME  = "Inovice Data"
WATCH_FOLDER_ID   = "1W-DeMNZZK232LTEzlJvSoQuz-2xG8xpH"
DONE_FOLDER_ID    = "1082crwrANAkcEl2_TUQb0b7oH25gqyil"
CHECK_INTERVAL    = 15
# ─────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]


def get_credentials():
    return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)


def get_drive_service():
    return build("drive", "v3", credentials=get_credentials())


def list_new_files(service, processed_ids):
    query = (
        f"'{WATCH_FOLDER_ID}' in parents and trashed=false and ("
        "mimeType='application/pdf' or "
        "mimeType='image/jpeg' or "
        "mimeType='image/png'"
        ")"
    )
    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType)"
    ).execute()
    files = results.get("files", [])
    return [f for f in files if f["id"] not in processed_ids]


def download_file(service, file_id, filename):
    ext = filename.split(".")[-1].lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    request = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(tmp, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    tmp.close()
    return tmp.name


def move_to_done(service, file_id):
    """Move file from watch/ to done/ folder."""
    # Get current parents
    file = service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file.get("parents", []))

    # Move by adding done folder and removing watch folder
    service.files().update(
        fileId=file_id,
        addParents=DONE_FOLDER_ID,
        removeParents=previous_parents,
        fields="id, parents"
    ).execute()
    print(f"  → Moved to done/")


def file_to_base64_image(file_path):
    ext = file_path.split(".")[-1].lower()
    if ext == "pdf":
        doc = fitz.open(file_path)
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_bytes = pix.tobytes("png")
        doc.close()
    else:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
    return base64.b64encode(img_bytes).decode("utf-8")


def extract_invoice_data(base64_image):
    client = Groq(api_key=GROQ_API_KEY)
    prompt = """Extract invoice data from this image and return ONLY valid JSON. No extra text.

{
    "invoice_number": "",
    "invoice_date": "",
    "vendor_name": "",
    "vendor_gstin": "",
    "customer_name": "",
    "subtotal": "",
    "tax": "",
    "total_amount": "",
    "currency": "INR",
    "payment_terms": ""
}

If a field is not found, use empty string."""

    response = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{base64_image}"}
                    },
                    {"type": "text", "text": prompt}
                ]
            }
        ],
        temperature=0
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def push_to_sheets(data, filename):
    creds = get_credentials()
    client = gspread.authorize(creds)
    try:
        sheet = client.open(SPREADSHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        print(f"  ERROR: Sheet '{SPREADSHEET_NAME}' not found.")
        return
    if sheet.row_count == 0 or sheet.acell("A1").value is None:
        headers = [
            "File Name", "Invoice Number", "Invoice Date",
            "Vendor Name", "Vendor GSTIN", "Customer Name",
            "Subtotal", "Tax", "Total Amount", "Currency", "Payment Terms"
        ]
        sheet.append_row(headers)
    row = [
        filename,
        data.get("invoice_number", ""),
        data.get("invoice_date", ""),
        data.get("vendor_name", ""),
        data.get("vendor_gstin", ""),
        data.get("customer_name", ""),
        data.get("subtotal", ""),
        data.get("tax", ""),
        data.get("total_amount", ""),
        data.get("currency", ""),
        data.get("payment_terms", ""),
    ]
    sheet.append_row(row)
    print(f"  ✅ Data pushed to Google Sheet")


def process_file(service, file):
    print(f"\n📄 New file: {file['name']}")

    print("  → Downloading...")
    local_path = download_file(service, file["id"], file["name"])

    print("  → Converting to image...")
    b64 = file_to_base64_image(local_path)

    print("  → Sending to Groq AI...")
    data = extract_invoice_data(b64)
    print(f"  → Extracted: {json.dumps(data, indent=2)}")

    print("  → Pushing to Google Sheets...")
    push_to_sheets(data, file["name"])

    print("  → Moving to done/...")
    move_to_done(service, file["id"])

    os.unlink(local_path)  # delete temp file
    print(f"  ✅ Done: {file['name']}")


def watch():
    print("🚀 Invoice Pipeline Started")
    print(f"👀 Watching watch/ folder every {CHECK_INTERVAL} seconds...\n")
    service = get_drive_service()
    processed = set()

    while True:
        try:
            new_files = list_new_files(service, processed)
            if new_files:
                for file in new_files:
                    try:
                        process_file(service, file)
                        processed.add(file["id"])
                    except Exception as e:
                        print(f"  ❌ Error processing {file['name']}: {e}")
            else:
                print(".", end="", flush=True)
        except Exception as e:
            print(f"\n⚠️  Connection error: {e}. Retrying...")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    watch()
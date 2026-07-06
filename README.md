# Gems Ektrator Automation

Upload a screenshot of the mission target/reward section and get the spreadsheet filled automatically.

Pipeline: EasyOCR -> image-enhancement retry -> Gemini Vision fallback -> write results into Google Sheets.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Create your local secrets file from the template:

   ```bash
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```

   Fill in:
   - `gcp_service_account` — a Google Cloud service account (JSON key) with edit access to the target spreadsheet. Share the sheet with the service account's `client_email`.
   - `GEMINI_API_KEY` (optional) — pre-fills the sidebar so you don't have to paste a key every session. You can leave it blank and type a key in the UI instead.
   - `SPREADSHEET_URL` (optional) — overrides the spreadsheet hardcoded in `app.py`.

   `.streamlit/secrets.toml` is git-ignored — never commit real credentials.

3. Run locally:

   ```bash
   streamlit run app.py
   ```

## Deployment

This is a stateful Streamlit server (loads an EasyOCR/torch model at startup, keeps a live connection to Google Sheets), not a stateless serverless function — so **Vercel is not a good fit**: its Python runtime is built for short-lived serverless functions with tight package-size limits, and doesn't run a persistent Streamlit process.

Recommended instead:

- **[Streamlit Community Cloud](https://streamlit.io/cloud)** — free, deploys straight from this GitHub repo, and its secrets manager maps directly to `st.secrets` already used in `app.py`. Easiest option.
- **[Hugging Face Spaces](https://huggingface.co/spaces)** (Streamlit SDK) — handles heavier ML dependencies (`easyocr`/`torch`) comfortably on its free CPU tier.
- **Render / Railway / Fly.io** (Docker) — if you want more control over resources or need the app always-on.

## Notes

- The "This week" sheet has a known data-entry typo (`Wedesday`), which the app currently normalizes at read-time (see comment in `app.py`). Worth fixing at the source too.
- Gemini is used only as a fallback when OCR (plain and enhanced) both fail to find exactly 3 gems/reward pairs.

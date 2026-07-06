import os
import re
import json
import time
import random
import easyocr
import gspread
import pandas as pd
import streamlit as st
from datetime import datetime
from PIL import Image, ImageEnhance

from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ============== KONFIGURASI HALAMAN ==============
st.set_page_config(page_title="Gems Automator", page_icon="💎", layout="wide")

CURRENT_YEAR = datetime.now().year  # used when the screenshot/filename has no year in it

DEFAULT_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1W6t8fCOPB_dFlWnzHSpVJ66tAnOcHHnMn1jXtHfC2S4/edit?usp=sharing"
# Allow overriding via .streamlit/secrets.toml so the same code can point at a
# different sheet per deployment without editing the source.
SPREADSHEET_URL = st.secrets.get("SPREADSHEET_URL", DEFAULT_SPREADSHEET_URL) if hasattr(st, "secrets") else DEFAULT_SPREADSHEET_URL

# ============== KONEKSI SPREADSHEET VIA SERVICE ACCOUNT ==============
@st.cache_resource
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SPREADSHEET_URL)
    return sh.worksheet("This week")

# ============== CACHE ENGINE OCR ==============
@st.cache_resource(show_spinner="Memuat Engine OCR... (Mohon tunggu sebentar)")
def load_ocr_engine():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr_engine()

# ============== HELPER: FUNGSI OCR & EKSTRAKSI ==============
def enhance_image_for_ocr(path):
    img = Image.open(path).convert('L')
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    enhanced_path = "enhanced_" + os.path.basename(path)
    img.save(enhanced_path)
    return enhanced_path

def extract_gems_rewards(ocr_result_list):
    start_idx, end_idx = -1, len(ocr_result_list)
    for idx, text in enumerate(ocr_result_list):
        if 'target' in text.lower(): start_idx = idx
        if 'qualification' in text.lower() or 'criteria' in text.lower():
            end_idx = idx
            break

    target_section_text = ocr_result_list[start_idx + 1: end_idx]
    gems_found, rewards_found = [], []

    for text in target_section_text:
        if '%' in text: continue
        matches = re.findall(r'\d+(?:\.\d+)?', text)
        for m in matches:
            val = float(m)
            if val <= 0 or val > 200: continue
            if '.' in text or '$' in text or 's$' in text.lower(): rewards_found.append(val)
            elif val.is_integer() and val < 150: gems_found.append(int(val))

    return sorted(list(set(gems_found))), sorted(list(set(rewards_found))), target_section_text

def analisis_dengan_ultimate_retry(model, prompt, gambar_list, max_retry=5):
    delay = 10
    for i in range(max_retry):
        try:
            time.sleep(2)
            respon = model.generate_content([prompt] + gambar_list)
            return respon.text
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                wait_time = delay + random.uniform(0, 5)
                st.warning(f"⚠️ Server sibuk. Menunggu {wait_time:.1f} detik... (percobaan {i+1}/{max_retry})")
                time.sleep(wait_time)
                delay *= 2
            else:
                raise e
    raise Exception("Gagal total setelah percobaan maksimal.")

@st.cache_resource(show_spinner=False)
def resolve_gemini_model_name(api_key):
    """Resolve which Gemini model to use once per API key instead of calling
    list_models() on every single fallback invocation."""
    genai.configure(api_key=api_key)
    models = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
    return next((m for m in models if "1.5-flash" in m), next((m for m in models if "flash" in m), models[0]))

def extract_via_gemini(image_path, api_key):
    if not api_key: return [], []
    try:
        model_name = resolve_gemini_model_name(api_key)
        genai.configure(api_key=api_key)
        gemini_model = genai.GenerativeModel(model_name)
    except Exception as e:
        st.error(f"⚠️ Gagal setup Gemini: {e}")
        return [], []

    prompt = """Look at this screenshot of a delivery rider mission app.
Find the "TARGET & REWARD" section. It contains exactly 3 tiers, each with:
- a "gems" target number (a small whole number)
- a corresponding "S$" reward amount
Return ONLY a raw JSON object: {"gems": [g1, g2, g3], "rewards": [r1, r2, r3]}"""

    try:
        img = Image.open(image_path)
        teks_raw = analisis_dengan_ultimate_retry(gemini_model, prompt, [img], max_retry=3)
        clean_text = re.sub(r'^```json\s*|\s*```$', '', teks_raw.strip())
        data = json.loads(clean_text)
        gems = sorted([int(g) for g in data.get("gems", [])])
        rewards = sorted([float(r) for r in data.get("rewards", [])])
        if len(gems) == 3 and len(rewards) == 3: return gems, rewards
        return [], []
    except:
        return [], []

# ==============================================================================
# ATURAN TARGET CELLS (KEMBARAN) — VERSI FINAL
#   1) Dalam 1 tier yang sama: E-bike = Bicycle
#   2) Diamond & Sapphire: vehicle yang sama saling terhubung (Motorcycle-Diamond
#      = Motorcycle-Sapphire, dst), KECUALI Walker (berdiri sendiri per tier)
#   3) Ruby & Emerald: TIDAK ada hubungan lintas-tier sama sekali
# ==============================================================================
def get_target_cells(tier, vehicle):
    tier_l = tier.lower()
    vehicle_l = vehicle.lower()

    # Grup vehicle dalam 1 tier yang sama (aturan #1)
    if vehicle_l in ['e-bike', 'bicycle']:
        same_tier_group = ['E-bike', 'Bicycle']
    else:
        same_tier_group = [vehicle.capitalize()]

    targets = [(tier, v) for v in same_tier_group]

    # Cross-tier Diamond <-> Sapphire (aturan #2), Walker dikecualikan
    if tier_l in ['diamond', 'sapphire'] and vehicle_l != 'walker':
        other_tier = 'Sapphire' if tier_l == 'diamond' else 'Diamond'
        for v in same_tier_group:
            targets.append((other_tier, v))

    # Hilangkan duplikat sambil jaga urutan
    seen = set()
    unique_targets = []
    for t in targets:
        key = (t[0].lower(), t[1].lower())
        if key not in seen:
            seen.add(key)
            unique_targets.append(t)

    return unique_targets

# ============== UI UTAMA ==============
st.title("💎 Automasi Ekstraksi Gems & Insentif")
st.caption("Upload *screenshot* -> diekstrak dengan EasyOCR/Gemini -> masuk otomatis ke Spreadsheet.")

# ============== KONEKSI SPREADSHEET (SERVICE ACCOUNT, TANPA LOGIN) ==============
try:
    sheet_this_week = get_sheet()
    st.success("✅ Terhubung dengan Google Sheets!")
except Exception as e:
    st.error(f"❌ Gagal membuka spreadsheet. Pastikan link benar dan Service Account punya akses. Error: {e}")
    st.stop()

st.divider()

# ============== KONFIGURASI SIDEBAR ==============
st.sidebar.header("⚙️ Pengaturan")
# Optional default so collaborators with a key in secrets.toml don't have to
# paste it in every session; anyone else can still type their own key.
_default_gemini_key = st.secrets.get("GEMINI_API_KEY", "") if hasattr(st, "secrets") else ""
gemini_api = st.sidebar.text_input(
    "Gemini API Key (Untuk Fallback Jika OCR Gagal)",
    value=_default_gemini_key,
    type="password",
)

tier_options = ['Diamond', 'Sapphire', 'Ruby', 'Emerald']
vehicle_options = ['Walker', 'Motorcycle', 'E-bike', 'Bicycle']

selected_tier = st.sidebar.selectbox("Pilih Tier", tier_options)
selected_vehicle = st.sidebar.selectbox("Pilih Vehicle", vehicle_options)

target_cells = get_target_cells(selected_tier, selected_vehicle)
target_display = [f"{t}-{v}" for t, v in target_cells]
st.sidebar.info(f"📌 **Target Utama:** {selected_tier} - {selected_vehicle}\n\n"
                f"🔄 **Auto-fill ke:** {', '.join(target_display)}")

# ============== UPLOAD & EKSEKUSI BATCH ==============
uploaded_files = st.file_uploader(
    "Unggah Gambar (Bisa sekalian banyak/batch)",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True
)

if uploaded_files and st.button("🚀 Jalankan Ekstraksi", type="primary"):
    temp_dir = "temp_uploads"
    os.makedirs(temp_dir, exist_ok=True)

    all_rows_this_week = sheet_this_week.get_all_values()
    progress_bar = st.progress(0)

    for idx, uploaded_file in enumerate(uploaded_files):
        filename = uploaded_file.name

        with st.expander(f"⚙️ Memproses: {filename}", expanded=True):
            files_to_cleanup = []
            temp_path = os.path.join(temp_dir, filename)
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            files_to_cleanup.append(temp_path)

            img = Image.open(temp_path)
            if img.width > img.height:
                st.write("🔄 Rotasi gambar *landscape* agar tegak...")
                img_rotated = img.rotate(90, expand=True)
                temp_path = os.path.join(temp_dir, "prepared_" + filename)
                img_rotated.save(temp_path)
                files_to_cleanup.append(temp_path)

            result = reader.readtext(temp_path, detail=0)
            full_text = " ".join(result)

            formatted_date, extracted_day = None, None
            fname_match = re.search(r'(\d{1,2})[\s_]*([A-Za-z]{3,9})', os.path.splitext(filename)[0])

            if fname_match:
                try:
                    dt = datetime.strptime(f"{fname_match.group(1)} {fname_match.group(2)[:3].capitalize()} {CURRENT_YEAR}", "%d %b %Y")
                    extracted_day = dt.strftime("%A")
                    formatted_date = dt.strftime("%d/%m/%Y")
                except: pass

            if not formatted_date:
                date_match = re.search(r'(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})?', full_text)
                if date_match:
                    try:
                        tahun_angka = date_match.group(3) if date_match.group(3) else str(CURRENT_YEAR)
                        dt = datetime.strptime(f"{date_match.group(1)} {date_match.group(2)[:3].capitalize()} {tahun_angka}", "%d %b %Y")
                        extracted_day = dt.strftime("%A")
                        formatted_date = dt.strftime("%d/%m/%Y")
                    except: pass

            if not formatted_date:
                st.error("⚠️ Tanggal tidak terdeteksi. Melewati file ini.")
                for f_path in files_to_cleanup:
                    if os.path.exists(f_path): os.remove(f_path)
                continue
            else:
                st.write(f"📅 Waktu Terdeteksi: **{formatted_date} ({extracted_day})**")

            gems_found, rewards_found, _ = extract_gems_rewards(result)

            if len(gems_found) != 3 or len(rewards_found) != 3:
                st.warning("⚠️ Pembacaan normal gagal. Mencoba *Image Enhancement*...")
                enhanced_path = enhance_image_for_ocr(temp_path)
                files_to_cleanup.append(enhanced_path)
                res_v2 = reader.readtext(enhanced_path, detail=0, text_threshold=0.4, low_text=0.3)
                gems_found, rewards_found, _ = extract_gems_rewards(res_v2)

                if len(gems_found) != 3 or len(rewards_found) != 3:
                    st.warning("⚠️ Masih gagal. Mencoba *Fallback* ke Gemini Vision API...")
                    gems_found, rewards_found = extract_via_gemini(temp_path, gemini_api)

            if len(gems_found) == 3 and len(rewards_found) == 3:
                pairs = list(zip(gems_found, rewards_found))
            else:
                st.error("❌ Ekstraksi angka gagal total. Silakan cek gambar manual.")
                for f_path in files_to_cleanup:
                    if os.path.exists(f_path): os.remove(f_path)
                continue

            # ==================================================================
            # Mapping & Update ke Google Sheets — LOOPING KE SEMUA TARGET CELLS
            # (tiap target punya tier & vehicle sendiri, bukan cuma 1 tier)
            # ==================================================================
            target_day = extracted_day.lower()
            current_target_cells = get_target_cells(selected_tier, selected_vehicle)

            for cell_tier, cell_vehicle in current_target_cells:
                matched_row_indices = []
                for r_idx, row in enumerate(all_rows_this_week):
                    if r_idx == 0: continue
                    r_tier = row[0].strip().lower() if len(row) > 0 else ""
                    r_veh = row[1].strip().lower() if len(row) > 1 else ""
                    r_day = row[2].strip().lower() if len(row) > 2 else ""
                    # NOTE: "This week" sheet has a known typo ("Wedesday") for Wednesday.
                    # Fixing it here rather than in the sheet so re-typing doesn't silently
                    # break matching again; ideally fix the source cell too.
                    if r_day == "wedesday": r_day = "wednesday"

                    if r_tier == cell_tier.lower() and r_veh == cell_vehicle.lower() and r_day == target_day:
                        matched_row_indices.append(r_idx + 1)

                if len(matched_row_indices) >= 3:
                    update_payload = []
                    for i in range(3):
                        g_val, r_val = pairs[i][0], pairs[i][1]
                        inc = round(r_val / g_val, 2) if g_val > 0 else 0
                        update_payload.append([formatted_date, g_val, f"{r_val:.2f}", f"{inc:.2f}"])

                    start_row, end_row = matched_row_indices[0], matched_row_indices[2]
                    sheet_this_week.update(values=update_payload, range_name=f"D{start_row}:G{end_row}")

                    if cell_tier.lower() == selected_tier.lower() and cell_vehicle.lower() == selected_vehicle.lower():
                        st.success(f"✨ Data **{cell_tier}-{cell_vehicle}** berhasil masuk di baris {start_row}-{end_row}.")
                    else:
                        st.info(f"➡️ Data kembaran **{cell_tier}-{cell_vehicle}** otomatis diisi di baris {start_row}-{end_row}.")
                else:
                    st.error(f"❌ Baris kosong untuk {cell_tier}-{cell_vehicle} hari {extracted_day} tidak ditemukan.")

            # Bersihkan file sementara agar temp_uploads/ tidak menumpuk
            for f_path in files_to_cleanup:
                if os.path.exists(f_path): os.remove(f_path)

        progress_bar.progress((idx + 1) / len(uploaded_files))

    st.balloons()
    st.success("🎉 Seluruh gambar selesai diproses dan dimasukkan ke Spreadsheet!")
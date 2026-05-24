"""
fix_doctor_setup.py
===================
Run this script ONCE to fix two issues:

  1. Doctor phone number not recognized (number treated as patient)
  2. Doctor name not set correctly in clinic settings

HOW TO RUN:
  Make sure your .env file has SUPABASE_URL and SUPABASE_KEY set, then:
    python fix_doctor_setup.py

OR set env vars inline:
  SUPABASE_URL=https://xxx.supabase.co SUPABASE_KEY=eyJ... python fix_doctor_setup.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ ERROR: SUPABASE_URL and SUPABASE_KEY must be set.")
    print("   Add them to your .env file or export them before running.")
    sys.exit(1)

try:
    from supabase import create_client
except ImportError:
    print("❌ supabase-py not installed. Run: pip install supabase")
    sys.exit(1)

# ── Configuration — edit these values ────────────────────────────────────────
DOCTOR_PHONE  = "919326376895"    # country code (91) + 10-digit number
DOCTOR_NAME   = "Dr. Shweta Gupta"
# ─────────────────────────────────────────────────────────────────────────────

db = create_client(SUPABASE_URL, SUPABASE_KEY)

print("=" * 60)
print("  Clinic AI Agent — Doctor Setup Fix")
print("=" * 60)

# ── Step 1: Show all clients ──────────────────────────────────────────────────
print("\n📋 Fetching all clients from Supabase...\n")
clients_result = db.table("clients").select("id, name, doctor_name, contact_phone, whatsapp_phone_id, status").order("id").execute()

if not clients_result.data:
    print("❌ No clients found in the clients table.")
    print("   Make sure your Supabase project has the correct schema.")
    sys.exit(1)

clients = clients_result.data
for c in clients:
    print(f"  [{c['id']}] {c['name']}")
    print(f"       doctor_name  : {c.get('doctor_name') or '(not set)'}")
    print(f"       contact_phone: {c.get('contact_phone') or '(not set)'}")
    print(f"       phone_id     : {c.get('whatsapp_phone_id') or '(not set)'}")
    print(f"       status       : {c.get('status') or '(not set)'}")
    print()

# ── Step 2: Pick client to update ────────────────────────────────────────────
if len(clients) == 1:
    client_id = clients[0]["id"]
    print(f"✅ Only one client found — using client ID {client_id}")
else:
    try:
        client_id = int(input("Enter the client ID to update: ").strip())
    except ValueError:
        print("❌ Invalid ID.")
        sys.exit(1)

# Validate
client = next((c for c in clients if c["id"] == client_id), None)
if not client:
    print(f"❌ Client ID {client_id} not found.")
    sys.exit(1)

print(f"\n🔧 Updating client [{client_id}] — {client['name']}")
print(f"   Setting contact_phone → {DOCTOR_PHONE}")
print(f"   Setting doctor_name   → {DOCTOR_NAME}")

# ── Step 3: Update clients table ─────────────────────────────────────────────
try:
    db.table("clients").update({
        "contact_phone": DOCTOR_PHONE,
        "doctor_name":   DOCTOR_NAME,
    }).eq("id", client_id).execute()
    print("\n✅ clients table updated successfully")
except Exception as e:
    print(f"\n❌ Failed to update clients table: {e}")
    sys.exit(1)

# ── Step 4: Update clinic_settings table ─────────────────────────────────────
try:
    db.table("clinic_settings").upsert(
        {"client_id": client_id, "key": "doctor_name", "value": DOCTOR_NAME},
        on_conflict="client_id,key"
    ).execute()
    print("✅ clinic_settings.doctor_name updated")
except Exception as e:
    print(f"⚠️  clinic_settings update failed (non-critical): {e}")

# ── Step 5: Verify ────────────────────────────────────────────────────────────
print("\n🔍 Verifying changes...")
result = db.table("clients").select("id, name, doctor_name, contact_phone").eq("id", client_id).single().execute()
updated = result.data

if updated:
    print(f"\n  Client ID     : {updated['id']}")
    print(f"  Clinic name   : {updated['name']}")
    print(f"  Doctor name   : {updated['doctor_name']}")
    print(f"  Contact phone : {updated['contact_phone']}")

    doc_phone = (updated.get("contact_phone") or "").strip()
    phone_clean = doc_phone.lstrip("+").lstrip("0")
    doctor_check = DOCTOR_PHONE.lstrip("+").lstrip("0")

    if phone_clean == doctor_check or phone_clean.endswith(doctor_check[-10:]):
        print(f"\n✅ SUCCESS! Number {DOCTOR_PHONE} will now be recognized as the doctor.")
        print(f"   Messages from this number will route to doctor mode.")
        print(f"\n   📱 Test: Send a message from {DOCTOR_PHONE} to your clinic WhatsApp.")
        print(f"   The bot should respond with doctor commands (block slots, view appointments, etc.)")
    else:
        print(f"\n⚠️  Phone mismatch after update. Got: {doc_phone}")

# ── Step 6: Also check/fix clinic_settings doctor_name ───────────────────────
cs_result = db.table("clinic_settings").select("key, value").eq("client_id", client_id).eq("key", "doctor_name").execute()
if cs_result.data:
    print(f"\n✅ clinic_settings.doctor_name = '{cs_result.data[0]['value']}'")
    print("   (This is what appears in patient confirmation messages)")
else:
    print(f"\n⚠️  No doctor_name in clinic_settings — patients will see default value from env var")

print("\n" + "=" * 60)
print("  Setup complete!")
print("=" * 60)
print("""
IMPORTANT: If your bot is deployed on Railway, you ALSO need to add
this environment variable in Railway → Variables:

  DOCTOR_PHONE = 919326376895

This serves as a fallback in case the Supabase clients table is
ever wiped or the contact_phone field is cleared.

Go to: Railway Dashboard → Your Project → Variables → Add Variable
""")

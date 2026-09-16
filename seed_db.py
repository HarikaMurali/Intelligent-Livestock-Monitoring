import os
import certifi
import pandas as pd
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
MONGO_DB = os.getenv('MONGO_DB', 'cattle_monitoring')

print("Connecting to MongoDB...")
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[MONGO_DB]

EXCEL_FILE = "Livestock_Monitoring_Dataset.xlsx"
ROW_COUNT = 50

# NEW: Helper function to prevent Excel date crashes!
def safe_date(val):
    if pd.isna(val): 
        return ""
    if isinstance(val, str): 
        return val.split(' ')[0] # If it's already a string, just return it
    try: 
        return val.strftime('%Y-%m-%d')
    except: 
        return str(val)

def seed_database():
    print("Clearing old data...")
    db.users.delete_many({})
    db.farms.delete_many({})
    db.cattle.delete_many({})
    db.settings.delete_many({})
    db.estrus.delete_many({})
    db.vaccinations.delete_many({})
    db.alerts.delete_many({})

    now = datetime.now(timezone.utc).isoformat()

    # 1. System Settings
    db.settings.insert_one({
        "estrus_alert_days": 2,
        "vaccination_alert_days": 2,
        "presence_timeout_minutes": 1
    })

    # 2. Users
    admin_id = db.users.insert_one({
        "name": "System Admin",
        "email": "admin@farm.com",
        "password_hash": generate_password_hash("admin123"),
        "role": "admin",
        "farm_ids": [],
        "active": True,
        "created_at": now
    }).inserted_id

    farmer_id = db.users.insert_one({
        "name": "John Farmer",
        "email": "farmer@farm.com",
        "password_hash": generate_password_hash("farmer123"),
        "role": "farmer",
        "farm_ids": ["F001", "F002"],
        "active": True,
        "created_at": now
    }).inserted_id

    # 3. Farms
    db.farms.insert_many([
        {
            "farm_id": "F001",
            "name": "Green Valley Farm",
            "location": "Bangalore",
            "owner_id": str(farmer_id),
            "status": "active",
            "created_at": now
        },
        {
            "farm_id": "F002",
            "name": "Sunrise Dairy",
            "location": "Mysore",
            "owner_id": str(farmer_id),
            "status": "active",
            "created_at": now
        }
    ])
    print("✅ System Core, Users, and Farms created.")

    # 4. Import Data from Excel Dataset
    print(f"\nReading {EXCEL_FILE}...")
    
    try:
        # A. CATTLE MASTER
        df_cattle = pd.read_excel(EXCEL_FILE, sheet_name='Cattle_Master').head(ROW_COUNT)
        cattle_inserts = []
        for _, row in df_cattle.iterrows():
            age_years = float(row['Age (yrs)']) if pd.notnull(row['Age (yrs)']) else 3.0
            dob = (datetime.now(timezone.utc) - timedelta(days=age_years * 365)).strftime('%Y-%m-%d')
            
            clean_rfid = str(row['RFID_Tag']).upper().replace('RFID', 'TAG-')

            cattle_inserts.append({
                "cattle_id": str(row['Cow_ID']).strip().upper(),
                "farm_id": "F001", 
                "rfid_uid": clean_rfid,
                "name": str(row['Name']).strip(),
                "breed": str(row['Breed']).strip(),
                "gender": "female",
                "date_of_birth": dob,
                "weight": round(float(row['Weight (kg)']), 1) if pd.notnull(row['Weight (kg)']) else 400.0,
                "health_status": str(row['Current_Health_Status']).strip().lower(),
                "presence_status": "present",
                "last_seen": now,
                "last_location": f"Zone {row['Farm_Section']}",
                "notes": str(row['Remarks']).strip(),
                "created_at": now,
                "updated_at": now
            })
        if cattle_inserts:
            db.cattle.insert_many(cattle_inserts)
            print(f"✅ Imported {len(cattle_inserts)} cattle profiles.")

        # B. REPRODUCTIVE HISTORY (ESTRUS)
        df_repro = pd.read_excel(EXCEL_FILE, sheet_name='Reproductive_History').head(ROW_COUNT)
        estrus_inserts = []
        for _, row in df_repro.iterrows():
            # Using the new safe_date function here!
            last_estrus = safe_date(row['Last_Estrus_Date'])
            next_estrus = safe_date(row['Next_Predicted_Estrus'])
            breeding = safe_date(row['Breeding_Date'])

            estrus_inserts.append({
                "cattle_id": str(row['Cow_ID']).strip().upper(),
                "last_estrus_date": last_estrus,
                "expected_next_date": next_estrus,
                "cycle_length_days": int(row['Cycle_Length_days']) if pd.notnull(row['Cycle_Length_days']) else 21,
                "breeding_date": breeding if breeding else None,
                "notes": str(row['Notes']) if pd.notnull(row['Notes']) else "",
                "updated_at": now
            })
        if estrus_inserts:
            db.estrus.insert_many(estrus_inserts)
            print(f"✅ Imported {len(estrus_inserts)} estrus records.")

        # C. VACCINATION RECORDS
        df_vax = pd.read_excel(EXCEL_FILE, sheet_name='Vaccination_Records').head(ROW_COUNT)
        vax_inserts = []
        for _, row in df_vax.iterrows():
            # Using the new safe_date function here too!
            vax_date = safe_date(row['Date_Administered'])
            next_due = safe_date(row['Next_Due_Date'])

            vax_inserts.append({
                "cattle_id": str(row['Cow_ID']).strip().upper(),
                "vaccine_name": str(row['Vaccine_Name']).strip(),
                "vaccination_date": vax_date,
                "next_due_date": next_due,
                "notes": f"{row.get('Remarks', '')} | Administered by: {row.get('Administered_By', 'Unknown')}",
                "created_at": now
            })
        if vax_inserts:
            db.vaccinations.insert_many(vax_inserts)
            print(f"✅ Imported {len(vax_inserts)} vaccination records.")

    except Exception as e:
        print(f"\n⚠️ Warning: Could not read Excel file data. Error: {e}")
        print("Please ensure 'Livestock_Monitoring_Dataset.xlsx' is in the same directory.")

    print("\n✅ Database successfully seeded!")
    print("--------------------------------------------------")
    print("Admin Login  -> Email: admin@farm.com  | Pass: admin123")
    print("Farmer Login -> Email: farmer@farm.com | Pass: farmer123")

if __name__ == "__main__":
    seed_database()
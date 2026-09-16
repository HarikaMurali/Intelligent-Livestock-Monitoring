# IoT-Based Smart Cattle Monitoring System

An IoT-based cattle monitoring and farm management system built with Flask, MongoDB Atlas, ESP32, RFID, temperature sensors, and OneSignal notifications.

The system allows farmers to manage cattle records, monitor temperature and presence, track vaccinations and estrus cycles, and receive health alerts.

## Features

### Web Application

- Admin and farmer login
- Role-based access control
- Farm management
- Farmer-to-farm assignment
- Cattle registration
- RFID-based cattle identification
- Cattle health records
- Temperature and presence status
- Vaccination records
- Estrus-cycle records
- Health and estrus alerts
- Analytics dashboard
- IoT gateway status page

### Hardware Integration

The planned hardware system includes:

- ESP32 or NodeMCU
- RFID reader and RFID ear tags
- DS18B20 temperature sensor
- RF transmitter and receiver modules
- Wi-Fi communication with the Flask server

The hardware communicates with Flask through HTTP API endpoints.

## Architecture

```text
RFID Reader / Temperature Sensor / RF Module
                    |
                    v
              ESP32 / NodeMCU
                    |
             Wi-Fi HTTP requests
                    |
                    v
             Flask Web Server
                    |
          ------------------------
          |                      |
          v                      v
      MongoDB Atlas          OneSignal
       Database             Notifications
          |
          v
      Web Dashboard

#Technology Stack
##Backend
Python
Flask
PyMongo
Werkzeug
Python Dotenv
Requests
Certifi

##Database
MongoDB Atlas

##Frontend
Jinja2 templates
HTML
Tailwind-style utility classes
JavaScript
Phosphor icons

##Hardware
ESP32 or NodeMCU
RFID reader
RFID tags
DS18B20 temperature sensor
RF transmitter and receiver

#Project Structure:
cattle/
├── app.py
├── seed_db.py
├── noti_test.py
├── requirements.txt
├── Cattle_Draft_Synopsis.txt
├── Livestock_Monitoring_Dataset.xlsx
├── .env
├── .gitignore
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── admin_dashboard.html
    ├── admin_farmers.html
    ├── admin_farms.html
    ├── admin_settings.html
    ├── farmer_dashboard.html
    ├── farmer_settings.html
    ├── farmer_analytics.html
    ├── farm_view.html
    ├── cattle_details.html
    ├── add_cattle.html
    ├── alerts.html
    ├── iot.html
    ├── estrus_form.html
    └── vaccination_form.html


#Environment Variables
Create a .env file in the project root:

MONGO_URI=mongodb+srv://<username>:<password>@<cluster>/<database>
MONGO_DB=cattle_monitoring
SECRET_KEY=replace-with-a-long-random-secret
ONESIGNAL_APP_ID=your-onesignal-app-id
ONESIGNAL_REST_API_KEY=your-onesignal-rest-api-key

#Database Setup:
The project uses MongoDB Atlas.

Create a MongoDB Atlas cluster.
Create a database user.
Add your current IP address to the Atlas network access list.
Put the MongoDB connection string in .env.
Ensure the Livestock_Monitoring_Dataset.xlsx file is in the project root.
Run the seed script: python seed_db.py
Warning!
seed_db.py deletes existing data from these collections before inserting new data.

#Running the Application
Start the Flask server:
python app.py
# TV Reminder Web Dashboard

A Flask-based web interface for managing your TV show reminders on your Raspberry Pi.

## Installation

### 1. Create Virtual Environment

```bash
cd /home/sherbert/tv-reminder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running the Web Dashboard

### Option A: As a Systemd Service (Recommended for Pi)

This runs the web app automatically on boot and restarts if it crashes.

```bash
bash setup_service.sh
```

Then access it at: `http://raspberrypi.local:5003`

**Useful commands:**
```bash
sudo systemctl status tv-reminder      # Check status
sudo systemctl restart tv-reminder     # Restart service
sudo systemctl stop tv-reminder        # Stop service
sudo systemctl start tv-reminder       # Start service
sudo journalctl -u tv-reminder -f      # View live logs
```

### Option B: Manual Run

For testing or development:

```bash
cd /home/sherbert/tv-reminder
source venv/bin/activate
python3 app.py
```

Then access it at: `http://localhost:5003` or `http://raspberrypi.local:5003`

## Architecture: Service vs Cron

Your system now has **two separate components** that work independently:

### 🌐 Web Dashboard (Systemd Service - Port 5003)
- Runs **continuously** in the background
- Shows upcoming episodes
- Lets you add/remove shows
- Allows manual reminder checks
- Automatically restarts if it crashes
- **Replaces** running `app.py` manually

### 📧 Email Reminders (Existing Cron Job)
- Runs on a **schedule** (e.g., every morning)
- Checks for new episodes
- Sends email notifications
- Continues working independently
- **You can keep this unchanged**

Both can run simultaneously without conflicts - they share the same `shows.yaml` and `state.json` files.

## Features

- **📺 Show Management**: Add and remove TV shows from your tracking list
- **🔄 Configuration**: Adjust your region (GB, US, AU, CA, IE) and how many days ahead to check
- **📅 Upcoming Episodes**: View all upcoming episodes for your tracked shows
- **🚀 Manual Checks**: Trigger reminder checks manually and send emails immediately
- **Auto-refresh**: Upcoming episodes list refreshes automatically every 5 minutes

## API Endpoints

- **GET /api/shows** - Get list of tracked shows
- **POST /api/shows** - Add a new show (JSON body: `{"name": "Show Name"}`)
- **DELETE /api/shows/<name>** - Remove a show from tracking
- **GET /api/upcoming** - Get upcoming episodes
- **POST /api/check** - Trigger reminder check and send emails
- **GET /api/config** - Get current configuration (region, days_ahead)
- **POST /api/config** - Update configuration (JSON body: `{"region": "GB", "days_ahead": 7}`)

## Data Files

- `shows.yaml` - List of shows to track (shared with cron job)
- `state.json` - Tracks which reminders have been sent (prevents duplicates)
- `config.env` - Email configuration (SMTP settings)

## Troubleshooting

**Port already in use?**
```bash
sudo lsof -i :5003
```

**Service won't start?**
```bash
sudo journalctl -u tv-reminder -n 50
```

**Need to update shows.yaml while service runs?**
The service reads the file dynamically - just edit and refresh the dashboard.

## Tips

1. Both the cron job and web dashboard use the same files, so they stay in sync
2. The web dashboard is great for manual checks and quick management
3. Keep the cron job for scheduled daily reminders
4. Logs are available via: `sudo journalctl -u tv-reminder -f`

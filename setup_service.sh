#!/bin/bash
# Setup script for TV Reminder service

echo "📺 Setting up TV Reminder web dashboard as systemd service..."
echo ""

# Copy service file to systemd directory
echo "1️⃣  Installing systemd service..."
sudo cp /home/sherbert/tv-reminder/tv-reminder.service /etc/systemd/system/

# Reload systemd daemon
echo "2️⃣  Reloading systemd..."
sudo systemctl daemon-reload

# Enable the service to start on boot
echo "3️⃣  Enabling service to start on boot..."
sudo systemctl enable tv-reminder.service

# Start the service
echo "4️⃣  Starting the service..."
sudo systemctl start tv-reminder.service

# Check status
echo ""
echo "✅ Setup complete! Service status:"
sudo systemctl status tv-reminder.service

echo ""
echo "📍 Access your dashboard at: http://raspberrypi.local:5003"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status tv-reminder      # Check status"
echo "  sudo systemctl restart tv-reminder     # Restart service"
echo "  sudo systemctl stop tv-reminder        # Stop service"
echo "  sudo systemctl start tv-reminder       # Start service"
echo "  sudo journalctl -u tv-reminder -f      # View live logs"
echo ""

#!/bin/sh
echo "Cloning qBitban repository..."
git clone https://github.com/srevn/qbitban
cd qbitban
echo "Creating virtual python environment..."
python -m venv venv
source venv/bin/activate
echo "Installing requirements..."
pip install -r requirements.txt
pip install pyinstaller
echo "Building qBitban..."
pyinstaller --name qbitban qbitban.py
chmod +x qbitban
chmod +x dist/qbitban/qbitban
echo "Moving items..."
cp -r dist/qbitban /usr/local/bin/
cp qbitban.json /usr/local/etc/
cp qbitban /usr/local/etc/rc.d/
echo "Cleaning up..."
cd .. && rm -rf qbitban

echo "To enable run 'sysrc qbitban_enable=YES' and 'service qbitban start'"
echo "Add details to configuration file in '/usr/local/etc/qbitban.json'"
#!/bin/sh
git clone https://github.com/asrevni/qbitban
cd qbitban
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --name qbitban qbitban.py
chmod +x qbitban
chmod +x dist/qbitban/qbitban
mv dist/qbitban /usr/local/bin/
mv qbitban.json /usr/local/etc/
mv qbitban /usr/local/etc/rc.d/
cd .. && rm -rf qbitban

echo "To enable run 'sysrc qbitban_enable=YES' and 'service qbitban start'"
echo "Add details to configuration file in '/usr/local/etc/qbitban.json'"
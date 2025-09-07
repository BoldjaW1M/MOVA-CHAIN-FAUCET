## Clone this repo
```sh
git clone https://github.com/USERNAME/mova-faucet-bot.git
cd mova-faucet-bot
```
# Install dependencies
```sh
pip install playwright==1.46.0
```
```sh
python -m playwright install chromium
```
# Prepare input files:
addresses.txt → list of EVM wallet addresses
proxies.txt → list of proxies (http://user:pass@ip:port)

# Run the bot:
```sh
python main.py
```

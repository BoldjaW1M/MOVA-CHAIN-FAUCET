# wallet_gen.py — Generate EVM wallets
# Save private keys to pvkey.txt, addresses to address.txt

from web3 import Web3
from eth_account import Account
import os

# Jumlah wallet yang mau dibuat
NUM_WALLETS = 10000

PVKEY_FILE = "pvkey.txt"
ADDR_FILE = "address.txt"

def main():
    pvkeys = []
    addrs = []

    for i in range(NUM_WALLETS):
        acct = Account.create()  # bikin wallet random
        pvkeys.append(acct.key.hex())
        addrs.append(acct.address)
        print(f"[{i+1}] {acct.address}  |  {acct.key.hex()}")

    # Simpan private keys
    with open(PVKEY_FILE, "a") as f:
        for k in pvkeys:
            f.write(k + "\n")

    # Simpan addresses
    with open(ADDR_FILE, "a") as f:
        for a in addrs:
            f.write(a + "\n")

    print(f"\n✅ Done! {NUM_WALLETS} wallets generated.")
    print(f"Private keys saved to {PVKEY_FILE}")
    print(f"Addresses saved to {ADDR_FILE}")

if __name__ == "__main__":
    main()

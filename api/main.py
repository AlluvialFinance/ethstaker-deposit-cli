from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import json
import tempfile
import time
import requests
import os
import shutil
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from ethstaker_deposit.credentials import CredentialList
from ethstaker_deposit.settings import get_devnet_chain_setting, get_chain_setting, KURTOSIS
from ethstaker_deposit.utils.constants import WORD_LISTS_PATH
from ethstaker_deposit.key_handling.key_derivation.mnemonic import get_mnemonic

app = FastAPI(title="Ethereum Validator Key API", 
             description="API for generating validator keys for Ethereum",
             version="0.1.0")

VALIDATOR_NODE_URL = os.environ.get("VALIDATOR_NODE_URL", "http://127.0.0.1:61214")
VALIDATOR_NODE_BEARER_TOKEN = os.environ.get("VALIDATOR_NODE_BEARER_TOKEN", "0x3ec0ad340bb9ca21e5593045b533d11d1b6784e03468af01db621db1804c2f0f")

class ProvisionRequest(BaseModel):
    """
    Request body for provisioning a new validator.
    """
    fee_recipient_address: str = Field(..., description="Ethereum address for validator fee recipient")
    withdrawal_address: str = Field(..., description="Ethereum address for withdrawal credentials")
    amount: str = Field(..., description="Amount in wei (usually '64000000000000000000' for 64 ETH)")

class ProvisionResponse(BaseModel):
    """
    Response body for provisioning a new validator.
    """
    public_key: str
    signature: str
    deposit_data_root: str

@app.get("/")
async def root():
    """
    Root endpoint for the API.
    """
    return {"message": "Welcome to the Ethereum Validator Key API"}

@app.post("/provision", response_model=ProvisionResponse)
def provision(request: ProvisionRequest):
    """
    Endpoint for provisioning a new validator.
    """
    try:
             
        amount_wei = int(request.amount)
        amount_eth = amount_wei / (10**18)
        amount_gwei = amount_wei // (10**9)

        if amount_eth != 64.0:
            print(f"Warning: Non-standard deposit amount: {amount_eth} ETH")
        
        mnemonic = get_mnemonic(language="english", words_path=WORD_LISTS_PATH)
        
        credentials = CredentialList.from_mnemonic(
            mnemonic=mnemonic,
            mnemonic_password="",  # No password for simplicity
            num_keys=1,  # Just one validator
            amounts=[amount_gwei],  # Use the provided amount
            chain_setting=get_chain_setting(KURTOSIS),
            start_index=0,
            hex_withdrawal_address=request.withdrawal_address,
            compounding=True
        )
        
        keystore_password = "kurtosis-testnet"
        keystore_json = ""
        deposit_data = None
        
        with tempfile.TemporaryDirectory() as tmp_folder:
            keystore_filefolders = credentials.export_keystores(
                password=keystore_password,
                timestamp=time.time(),
                folder=tmp_folder
            )
            
            with open(keystore_filefolders[0], 'r') as f:
                keystore_json = json.loads(f.read())
            
            deposit_file = credentials.export_deposit_data_json(folder=tmp_folder, timestamp=time.time())
            with open(deposit_file, 'r') as f:
                deposit_data = json.load(f)
            
            os.makedirs('keystores', exist_ok=True)
            
            public_key = credentials.credentials[0].signing_pk.hex()
            permanent_keystore_path = os.path.join('keystores', f'{public_key}.json')
            shutil.copy(keystore_filefolders[0], permanent_keystore_path)
            print(f"Stored keystore at: {permanent_keystore_path}")
        
        keystore_imported = False
        try:
            validator_endpoint = f"{VALIDATOR_NODE_URL}/eth/v1/keystores"
            
            validator_request = {
                "keystores": [json.dumps(keystore_json)],
                "passwords": [keystore_password]
            }
            
            print(f"Sending request to validator node: {validator_endpoint}")
            print(f"Keystore for public key: {keystore_json.get('pubkey', 'unknown')}")
            
            response = requests.post(
                validator_endpoint,
                json=validator_request,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {VALIDATOR_NODE_BEARER_TOKEN}"}
            )
            
            print(f"Validator node response status: {response.status_code}")
            print(f"Validator node response: {response.text}")
            
            keystore_imported = response.status_code == 200
            if not keystore_imported:
                print(f"Failed to import keystore to validator node: {response.text}")
            
        except Exception as e:
            print(f"Error sending keystore to validator node: {str(e)}")
        
        # Extract signature and deposit_data_root from deposit data
        signature = deposit_data[0]['signature']
        deposit_data_root = deposit_data[0]['deposit_data_root']
        
        print(f"Storing fee recipient address {request.fee_recipient_address} for validator {public_key}")
        
        return {
            "public_key": public_key,
            "signature": signature,
            "deposit_data_root": deposit_data_root
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) 

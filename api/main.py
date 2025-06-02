from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.responses import Response as StarletteResponse # Renamed to avoid conflict
from pydantic import BaseModel, Field
import json
import tempfile
import time
import requests
import os
import shutil
from dotenv import load_dotenv
import logging
from pathlib import Path
from typing import Optional # For potential future use in ExitRequest

from blspy import PrivateKey
from ethstaker_deposit.key_handling.keystore import Keystore
from ethstaker_deposit.utils.exit_transaction import exit_transaction_generation
# Ensure eth2spec types like SignedVoluntaryExit are handled;
# create_signed_voluntary_exit_message returns a spec-typed object.
# Explicit import might be needed if type hinting its direct return:
# from eth2spec.phase0.mainnet import SignedVoluntaryExit as Phase0SignedVoluntaryExit

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load environment variables from .env file
load_dotenv()

from ethstaker_deposit.credentials import CredentialList
from ethstaker_deposit.settings import get_devnet_chain_setting, get_chain_setting, KURTOSIS
from ethstaker_deposit.utils.constants import WORD_LISTS_PATH
from ethstaker_deposit.key_handling.key_derivation.mnemonic import get_mnemonic

app = FastAPI(title="Ethereum Validator Key API", 
             description="API for generating validator keys for Ethereum",
             version="0.1.0")

@app.middleware("http")
async def log_requests_responses_middleware(request: Request, call_next):
    # Log request details
    logging.info(f"Incoming Request: {request.method} {request.url.path}")
    logging.info(f"Headers: {dict(request.headers)}")
    
    # Read and log request body
    request_body_bytes = await request.body()
    if request_body_bytes:
        try:
            request_body_json = json.loads(request_body_bytes.decode('utf-8'))
            logging.info(f"Request Body: {request_body_json}")
        except json.JSONDecodeError:
            logging.info(f"Request Body (not JSON): {request_body_bytes.decode(errors='ignore')}")
    else:
        logging.info("Request Body: Empty")

    # Process the request
    response = await call_next(request)

    # Log response details
    logging.info(f"Response Status Code: {response.status_code}")

    response_body_bytes = b""
    if isinstance(response, StreamingResponse):
        # Create an async generator to read the stream, log it, and pass it through
        async def body_iterator():
            nonlocal response_body_bytes
            async for chunk in response.body_iterator:
                response_body_bytes += chunk
                yield chunk
        
        # Consume the iterator to log the full body, then reconstruct the response
        # This temporary consumption is to get the full body for logging.
        # The client will receive data through the new StreamingResponse.
        temp_body_parts = []
        async for chunk in body_iterator():
            temp_body_parts.append(chunk)
        
        # Log the fully formed body
        if response_body_bytes:
            try:
                # Attempt to decode as JSON first for prettier logging
                response_body_json = json.loads(response_body_bytes.decode('utf-8'))
                logging.info(f"Response Body: {response_body_json}")
            except json.JSONDecodeError:
                logging.info(f"Response Body (not JSON or empty): {response_body_bytes.decode(errors='ignore')}")
        else:
            logging.info("Response Body: Empty or already streamed")

        # Re-create the generator for the actual response to be sent to the client
        async def new_body_iterator():
            for part in temp_body_parts:
                yield part

        # Return a new StreamingResponse that the client can consume
        return StreamingResponse(
            content=new_body_iterator(), 
            status_code=response.status_code,
            headers=dict(response.headers), 
            media_type=response.media_type
        )
    elif hasattr(response, "body"): # Handles FastAPI's JSONResponse, HTMLResponse etc.
        response_body_bytes = response.body
        if response_body_bytes:
            try:
                # Attempt to decode as JSON first for prettier logging
                response_body_json = json.loads(response_body_bytes.decode('utf-8'))
                logging.info(f"Response Body: {response_body_json}")
            except json.JSONDecodeError:
                logging.info(f"Response Body (not JSON): {response_body_bytes.decode(errors='ignore')}")
        else:
            logging.info("Response Body: Empty")
    else:
        logging.info("Response Body: Not directly accessible (possibly already sent or non-standard response type)")

    return response

VALIDATOR_NODE_URL = os.environ.get("VALIDATOR_NODE_URL", "http://127.0.0.1:61214")
VALIDATOR_NODE_BEARER_TOKEN = os.environ.get("VALIDATOR_NODE_BEARER_TOKEN", "0x3ec0ad340bb9ca21e5593045b533d11d1b6784e03468af01db621db1804c2f0f")
BEACON_NODE_URL = os.environ.get("BEACON_NODE_URL", "http://127.0.0.1:3500") # Default to a common beacon node port

class ProvisionRequest(BaseModel):
    """
    Request body for provisioning a new validator.
    """
    fee_recipient_address: str = Field(..., description="Ethereum address for validator fee recipient")
    withdrawal_address: str = Field(..., description="Ethereum address for withdrawal credentials")
    amount: int = Field(..., description="Amount in Gwei (e.g., 64 for 64 Gwei, which will be converted to 64000000000 Wei)")

class ProvisionResponse(BaseModel):
    """
    Response body for provisioning a new validator.
    """
    public_key: str
    signature: str
    deposit_data_root: str

class ExitRequest(BaseModel):
    """
    Request body for exiting a validator.
    """
    public_key: str = Field(..., description="The hex-encoded public key of the validator to exit.")
    # epoch: Optional[int] = Field(None, description="Epoch for the exit message (advanced). If None, current epoch is used.")

class ExitResponse(BaseModel):
    """
    Response body for the exit request.
    """
    status: str
    detail: str
    submitted_data: Optional[dict] = None

def get_validator_details(public_key: str, beacon_node_api_url: str, chain_setting_obj) -> tuple[int, int]:
    """
    Fetches validator index from its public key and the current epoch from the beacon node.
    """
    # Get validator index
    validator_api_url = f"{beacon_node_api_url}/eth/v1/beacon/states/head/validators/{public_key}"
    logging.info(f"Fetching validator details from: {validator_api_url}")
    try:
        headers = {"Authorization": f"Bearer {VALIDATOR_NODE_BEARER_TOKEN}"}
        response = requests.get(validator_api_url, headers=headers, timeout=10)
        response.raise_for_status()
        validator_data_response = response.json()
        logging.info(f"Raw validator data response from beacon node: {json.dumps(validator_data_response, indent=2)}")

        if 'data' not in validator_data_response or not isinstance(validator_data_response['data'], dict) or 'index' not in validator_data_response['data']:
            logging.error(f"Validator data for public key {public_key} is missing, not an object, or does not contain an index: {json.dumps(validator_data_response, indent=2)}")
            raise ValueError(f"Validator data for public key {public_key} not found or malformed")

        validator_index = int(validator_data_response['data']['index'])
        logging.info(f"Found validator index: {validator_index} for pubkey: {public_key}")

    except requests.RequestException as e:
        logging.error(f"HTTP error fetching validator index: {e}")
        raise HTTPException(status_code=503, detail=f"Error connecting to Beacon Node for validator info: {str(e)}")
    except (KeyError, IndexError, ValueError) as e:
        logging.error(f"Error parsing validator data for {public_key}: {e}")
        raise HTTPException(status_code=500, detail=f"Error parsing validator data: {str(e)}")

    # Get current epoch
    chain_head_url = f"{beacon_node_api_url}/eth/v1/beacon/headers/head"
    logging.info(f"Fetching current slot/epoch from: {chain_head_url}")
    try:
        headers = {"Authorization": f"Bearer {VALIDATOR_NODE_BEARER_TOKEN}"}
        response = requests.get(chain_head_url, headers=headers, timeout=10)
        response.raise_for_status()
        current_slot = int(response.json()['data']['header']['message']['slot'])
        # SLOTS_PER_EPOCH is available from chain_setting_obj
        SLOTS_PER_EPOCH = 32 # Standard value for most Ethereum networks including testnets like Hoodi
        current_epoch = current_slot // SLOTS_PER_EPOCH
        logging.info(f"Current slot: {current_slot}, current epoch: {current_epoch}")

    except requests.RequestException as e:
        logging.error(f"HTTP error fetching current epoch: {e}")
        raise HTTPException(status_code=503, detail=f"Error connecting to Beacon Node for epoch: {str(e)}")
    except (KeyError, ValueError) as e:
        logging.error(f"Error parsing chain head data: {e}")
        raise HTTPException(status_code=500, detail=f"Error parsing chain head data: {str(e)}")

    return validator_index, current_epoch

def signed_voluntary_exit_to_json(signed_exit) -> dict:
    """
    Serializes an SSZ SignedVoluntaryExit object to a JSON-compatible dict for the Beacon API.
    """
    return {
        "message": {
            "epoch": str(signed_exit.message.epoch),
            "validator_index": str(signed_exit.message.validator_index),
        },
        "signature": "0x" + signed_exit.signature.hex(),
    }

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
             
        amount_eth = request.amount
        amount_gwei = request.amount * 1000000000

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

@app.post("/exit", response_model=ExitResponse)
async def exit_validator(request: ExitRequest):
    """
    Endpoint for initiating a voluntary exit for a validator.
    """
    logging.info(f"Received exit request for public key: {request.public_key}")
    try:
        public_key = request.public_key
        if not public_key.startswith("0x"):
            public_key = "0x" + public_key

        # --- 1. Load Chain Setting ---
        chain_setting = get_chain_setting(KURTOSIS) # Assumes KURTOSIS and get_chain_setting are available
        logging.info(f"Using chain setting: {chain_setting.NETWORK_NAME}")

        # --- 2. Locate Keystore and Load Private Key ---
        keystore_password = "kurtosis-testnet"
        # Keystore filename from /provision is pubkey without 0x prefix
        keystore_filename = f"{public_key[2:] if public_key.startswith('0x') else public_key}.json"
        keystore_path = Path('keystores') / keystore_filename

        logging.info(f"Attempting to load keystore from: {keystore_path}")
        if not keystore_path.exists():
            logging.error(f"Keystore file not found: {keystore_path}")
            raise HTTPException(status_code=404, detail=f"Keystore for public key {public_key} not found at {keystore_path}")

        try:
            keystore_obj = Keystore.from_file(keystore_path)
            private_key_bytes = keystore_obj.decrypt(password=keystore_password)
            signing_key = PrivateKey.from_bytes(private_key_bytes)
            # Verify derived public key matches the request
            derived_pubkey_bytes = bytes(signing_key.get_g1())
            derived_pubkey_hex = "0x" + derived_pubkey_bytes.hex()
            if derived_pubkey_hex.lower() != public_key.lower():
                logging.error(f"Public key mismatch. Expected {public_key}, got {derived_pubkey_hex} from keystore {keystore_path}.")
                raise HTTPException(status_code=400, detail="Public key in request does not match key in keystore.")
            logging.info(f"Successfully loaded private key for public key: {public_key}")
        except Exception as e:
            logging.error(f"Failed to decrypt keystore or load private key for {keystore_path}: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail=f"Failed to decrypt keystore. Check password or keystore integrity. Error: {str(e)}")

        # --- 3. Get Validator Index and Current Epoch from Beacon Node ---
        beacon_node_api_url = BEACON_NODE_URL # Use dedicated Beacon Node URL
        validator_index, epoch_to_use = get_validator_details(public_key, beacon_node_api_url, chain_setting)
        logging.info(f"Using validator index: {validator_index}, epoch for exit: {epoch_to_use}")

        # --- 4. Create Signed Voluntary Exit Message ---
        signed_voluntary_exit = exit_transaction_generation(
            signing_key=int.from_bytes(bytes(signing_key), 'big'),
            validator_index=validator_index,
            epoch=epoch_to_use,
            chain_setting=chain_setting
        )
        logging.info(f"Successfully created signed voluntary exit message.")

        # --- 5. Submit Signed Exit to Beacon Node ---
        exit_submission_url = f"{beacon_node_api_url}/eth/v1/beacon/pool/voluntary_exits"
        signed_exit_json = signed_voluntary_exit_to_json(signed_voluntary_exit)

        logging.info(f"Submitting signed voluntary exit to: {exit_submission_url}")
        logging.debug(f"Submission payload: {json.dumps(signed_exit_json, indent=2)}")

        try:
            headers = {"Content-Type": "application/json"}
            # Add Authorization header if your beacon node requires it for this endpoint
            headers["Authorization"] = f"Bearer {VALIDATOR_NODE_BEARER_TOKEN}"
            response = requests.post(exit_submission_url, json=signed_exit_json, headers=headers, timeout=10)

            logging.info(f"Beacon node response status: {response.status_code}")
            logging.info(f"Beacon node response body: {response.text}")

            if response.status_code == 200:
                return ExitResponse(status="success", detail="Voluntary exit submitted successfully.", submitted_data=signed_exit_json)
            else:
                try:
                    error_detail = response.json()
                except json.JSONDecodeError:
                    error_detail = response.text
                raise HTTPException(status_code=response.status_code, detail=f"Beacon node rejected voluntary exit: {error_detail}")

        except requests.RequestException as e:
            logging.error(f"HTTP error submitting voluntary exit: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=f"Error submitting exit to Beacon Node: {str(e)}")

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error in /exit endpoint for pubkey {request.public_key}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")

# Ethereum Validator Key API

An API built on top of the staking-deposit-cli to programmatically generate Ethereum validator keys for local testnets.

## Setup

1. Install the API dependencies:
```bash
pip install -r requirements_api.txt
```

2. Run the API:
```bash
python -m api.run
```

This will start the FastAPI server at http://localhost:8000.

## API Endpoints

### `/provision` (POST)

Generate a new validator key for use with the local testnet.

**Request**:
```json
{
  "fee_recipient_address": "0x1234567890abcdef1234567890abcdef12345678",
  "withdrawal_address": "0xabcdef1234567890abcdef1234567890abcdef12",
  "amount": "32000000000"
}
```

**Response**:
```json
{
  "public_key": "0x123...",
  "deposit_data_json": "{...}",
  "deposit_data_root": "0x456..."
}
```

## Usage with Kurtosis

In order to run this against Kurtosis, you need to set the following environment variable:
```
export VALIDATOR_NODE_URL=
```

For example getting the RPC url for one of the valiadtor clients, such as : 
```
vc-1-geth-teku-lodestar's http-validator http url
```

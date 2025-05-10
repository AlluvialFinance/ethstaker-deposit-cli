# Ethereum Validator Key API

An API built on top of a fork of ethstaker-deposit-cli to programmatically generate Ethereum validator keys for kurtosis testnets.

## Setup

0. Install python via pyenv, and create a virtualenv for the project.

```bash 
pyenv install 3.12
pyenv local 3.12

pyenv virtualenv 3.12 ethstaker-deposit-cli 
pyenv activate ethstaker-deposit-cli 
```

1. Install the API dependencies:
```bash
pip install -r requirements_api.txt
```

2. Run the API:

In order to run this against Kurtosis, you need to set the following environment variable:
```
export VALIDATOR_NODE_URL=
```

This needs to be an RPC URL for one of the validator clients, such as : 
```
vc-1-geth-teku-lodestar's http-validator http url
```

The final command should look like this:
```bash
export VALIDATOR_NODE_URL='http://localhost:128102' && python -m api.run
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


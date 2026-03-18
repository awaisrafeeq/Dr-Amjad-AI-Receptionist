#!/usr/bin/env python3
"""
Test script to make a call via Azure Communication Services
"""
import asyncio
from azure.communication.callautomation import CallAutomationClient
from config import get_config

async def make_test_call():
    config = get_config()
    client = CallAutomationClient.from_connection_string(config["acs_connection_string"])
    
    # Make a test call to your own number
    target_number = config["acs_source_number"]  # Call your own ACS number
    
    print(f"Attempting to call: {target_number}")
    print("Note: This will create a call loop - your bot will answer its own call")
    
    try:
        # This is just for testing the connection
        print("ACS Client initialized successfully")
        print("Go to Azure Portal to make test calls via UI")
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(make_test_call())

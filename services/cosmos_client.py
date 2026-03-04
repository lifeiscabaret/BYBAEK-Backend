import os
from dotenv import load_dotenv
from azure.cosmos import CosmosClient

load_dotenv()

def get_cosmos_container(container_name: str):
    endpoint = os.environ["AZURE_COSMOS_URL"]
    key = os.environ["AZURE_COSMOS_KEY"]
    database_name = "BybaekDB"

    client = CosmosClient(endpoint, key)
    database = client.get_database_client(database_name)
    container = database.get_container_client(container_name)

    return container
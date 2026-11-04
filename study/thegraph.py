import httpx
import json

playground_url = "https://thegraph.com/explorer/api/playground/"


async def get_thegraph_data(query,subgraph_id, deployment_id):
    url = playground_url + deployment_id
    client = httpx.AsyncClient(timeout=30)

    headers = {
        "accept": "application/json, multipart/mixed",
        "content-type": "application/json",
        "origin": "https://thegraph.com",
        "referer": f"https://thegraph.com/explorer/subgraphs/{subgraph_id}?view=Query&chain=arbitrum-one",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }

    payload = {"query": query}
    response = await client.post(url, headers=headers, json=payload)
    data = response.json()

    return data

async def get_uniswap_v3_data(query):
    subgraph_id = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
    deployment_id = "QmTZ8ejXJxRo7vDBS4uwqBeGoxLSWbhaA7oXa1RvxunLy7"
    data = await get_thegraph_data(query,subgraph_id, deployment_id)
    return data.get("data", {}).get("pools", []) if data else []

async def get_uniswap_v2_data(query):
    subgraph_id = "A3Np3RQbaBA6oKJgiwDJeo5T3zrYfGHPWFYayMwtNDu"
    deployment_id = "QmZzsQGDmQFbzYkv2qx4pVnD6aVnuhKbD3t1ea7SAvV7zE"
    data = await get_thegraph_data(query,subgraph_id, deployment_id)
    return data.get("data", {}).get("pairs", []) if data else []

async def get_sushiswap_v2_data(query):
    subgraph_id = "GyZ9MgVQkTWuXGMSd3LXESvpevE8S8aD3uktJh7kbVmc"
    deployment_id = "QmaR2nAMF6dCHBL1eFNQ4F5nGpJQs7V11PZobJB2FgQtbt"
    data = await get_thegraph_data(query,subgraph_id, deployment_id)
    return data.get("data", {}).get("pairs", []) if data else []
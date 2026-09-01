import requests

BASE_URL = "http://192.168.50.216:85/chat/api/v1"
TOKEN = "sm_yDuDmqXPNCELKq2pk-Gc6In0D_s5-H_2-JEigbHw9CU"
# TOKEN = "sm_1dxny_km1gXHkHLSvtSyHcp8etysvFCCgF5zBX61_fk"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}


# # 1. Test API connection / list conversations
# r = requests.get(
#     f"{BASE_URL}/conversations.list",
#     headers=headers,
#     timeout=10,
# )

# print("Conversations:")
# print("Status:", r.status_code)
# print(r.text)


# # 2. Send message to a conversation
r = requests.post(
    f"{BASE_URL}/chat.postMessage",
    headers=headers,
    json={
        "channel": "73",
        "text": "Test message from Python",
    },
    timeout=10,
)

print("\nSend channel message:")
print("Status:", r.status_code)
print(r.text)


# # 3. Send DM to a user
# r = requests.post(
#     f"{BASE_URL}/chat.postMessage",
#     headers=headers,
#     json={
#         "user": "sachin",
#         "text": "Test DM from Python",
#     },
#     timeout=10,
# )
# print("\nSend DM:")
# print("Status:", r.status_code)
# print(r.text)


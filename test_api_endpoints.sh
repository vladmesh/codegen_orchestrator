#!/bin/bash
# Test script for Phase 3 API endpoints
# Usage: ./test_api_endpoints.sh

set -e

API_URL="${API_URL:-http://localhost:8000}"
SERVER_HANDLE="${SERVER_HANDLE:-vps-267179}"

echo "=========================================="
echo "Testing Server Provisioning API Endpoints"
echo "API URL: $API_URL"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Test 1: Create an incident
echo -e "${YELLOW}Test 1: Create incident${NC}"
INCIDENT_RESPONSE=$(curl -s -X POST "$API_URL/api/incidents/" \
  -H "Content-Type: application/json" \
  -d "{
    \"server_handle\": \"$SERVER_HANDLE\",
    \"incident_type\": \"server_unreachable\",
    \"details\": {\"error\": \"SSH timeout\"},
    \"affected_services\": []
  }")

INCIDENT_ID=$(echo $INCIDENT_RESPONSE | jq -r '.id')
echo -e "${GREEN}✓ Created incident ID: $INCIDENT_ID${NC}"
echo ""

# Test 2: List all incidents
echo -e "${YELLOW}Test 2: List all incidents${NC}"
curl -s "$API_URL/api/incidents/" | jq '.[] | {id, server_handle, incident_type, status}'
echo ""

# Test 3: Get active incidents
echo -e "${YELLOW}Test 3: Get active incidents${NC}"
curl -s "$API_URL/api/incidents/active" | jq '.[] | {id, status}'
echo ""

# Test 4: Update incident status
echo -e "${YELLOW}Test 4: Update incident to 'recovering'${NC}"
curl -s -X PATCH "$API_URL/api/incidents/$INCIDENT_ID" \
  -H "Content-Type: application/json" \
  -d '{"status": "recovering", "recovery_attempts": 1}' | jq '{id, status, recovery_attempts}'
echo ""

# Test 5: Force rebuild server
echo -e "${YELLOW}Test 5: Trigger FORCE_REBUILD for server${NC}"
curl -s -X POST "$API_URL/api/servers/$SERVER_HANDLE/force-rebuild" | jq '{handle, status}'
echo ""

# Test 6: Get server incidents
echo -e "${YELLOW}Test 6: Get server incident history${NC}"
curl -s "$API_URL/api/servers/$SERVER_HANDLE/incidents" | jq '.[] | {id, incident_type, status}'
echo ""

# Test 7: Provision server
echo -e "${YELLOW}Test 7: Trigger manual provisioning${NC}"
curl -s -X POST "$API_URL/api/servers/$SERVER_HANDLE/provision" | jq '.'
echo ""

# Test 8: Update server status via PATCH
echo -e "${YELLOW}Test 8: Update server status to 'ready'${NC}"
curl -s -X PATCH "$API_URL/api/servers/$SERVER_HANDLE" \
  -H "Content-Type: application/json" \
  -d '{"status": "ready"}' | jq '{handle, status}'
echo ""

# Test 9: Resolve incident
echo -e "${YELLOW}Test 9: Resolve incident${NC}"
CURRENT_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S")
curl -s -X PATCH "$API_URL/api/incidents/$INCIDENT_ID" \
  -H "Content-Type: application/json" \
  -d "{\"status\": \"resolved\", \"resolved_at\": \"$CURRENT_TIME\"}" | jq '{id, status, resolved_at}'
echo ""

echo -e "${GREEN}=========================================="
echo "All tests completed successfully!"
echo -e "==========================================${NC}"

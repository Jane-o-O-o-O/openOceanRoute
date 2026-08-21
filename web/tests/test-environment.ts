/** Explicit endpoint overrides keep browser and API checks on the same server. */
export const apiURL=(process.env.OCEANROUTE_E2E_API_URL||'http://127.0.0.1:8765/api').replace(/\/$/,'');
export const productionURL=(process.env.OCEANROUTE_E2E_BASE_URL||'http://127.0.0.1:8765').replace(/\/$/,'');

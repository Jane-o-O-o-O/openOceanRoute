import {defineConfig} from '@playwright/test';
import {fileURLToPath} from 'node:url';
const python=process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python';
export default defineConfig({testDir:'./tests',timeout:60000,workers:1,retries:0,use:{baseURL:'http://127.0.0.1:5173',viewport:{width:1512,height:982},channel:'chrome',headless:true,screenshot:'only-on-failure'},webServer:[{command:python+' -m oceanroute --port 8765',cwd:fileURLToPath(new URL('..',import.meta.url)),url:'http://127.0.0.1:8765/api/health',reuseExistingServer:true},{command:'npm run dev',url:'http://127.0.0.1:5173',reuseExistingServer:true}]});

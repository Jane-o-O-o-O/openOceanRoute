import { defineConfig,loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({mode})=>({
  plugins:[react()],
  server:{proxy:{'/api':loadEnv(mode,'.','OCEANROUTE_').OCEANROUTE_API_PROXY||'http://127.0.0.1:8765'}},
  build:{chunkSizeWarningLimit:1000,rollupOptions:{output:{manualChunks:{three:['three'],map:['leaflet'],react:['react','react-dom'],markdown:['react-markdown','remark-gfm']}}}}
}));

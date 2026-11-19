import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backend = { target: 'http://127.0.0.1:8000', changeOrigin: true };
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(['/tasks', '/health', '/projects', '/snapshots', '/jobs', '/history', '/auth']
      .map(path => [path, backend])),
  },
});

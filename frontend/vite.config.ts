import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../agent/_builtin/web/dist',
    emptyOutDir: true,
  },
})

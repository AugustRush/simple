import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // Development builds stay with the frontend source tree. The package
    // bundle is refreshed explicitly through `npm run build:release`.
    outDir: 'dist',
  },
})

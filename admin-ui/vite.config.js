import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 管理后台前端构建配置
// 开发：跨域代理到后端 :8001
// 构建：产物输出到 ../app/static/dist，FastAPI 静态托管于 /admin/ui
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5178,
    proxy: {
      '/admin': 'http://localhost:8001',
    },
  },
  build: {
    outDir: '../app/static/dist',
    emptyOutDir: true,
  },
})

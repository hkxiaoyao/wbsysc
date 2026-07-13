import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 管理后台前端构建配置
// 开发：跨域代理到后端 :8001
// 构建：产物输出到 ../app/static/dist，FastAPI 静态托管于 /admin/ui
export default defineConfig({
  plugins: [react()],
  // 与 FastAPI mount 路径一致：app.mount("/admin/ui", StaticFiles(...))
  // 不设 base 时构建产物会引用 /assets/*，部署后 404 白屏
  base: '/admin/ui/',
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

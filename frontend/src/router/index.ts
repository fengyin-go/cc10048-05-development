import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import BatchDrillView from '@/views/BatchDrillView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须排在 /:moduleKey 之前，否则会被动态路由抢先匹配。
    { path: '/batch_drill', name: 'batch_drill', component: BatchDrillView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router

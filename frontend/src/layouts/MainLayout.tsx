import React, { useEffect, useState } from 'react';
import { Outlet } from 'react-router-dom';
import { AppSidebar } from '../components/AppSidebar';
import { TopNavbar } from '../components/TopNavbar';
import { WorkflowJob } from '../components/WorkflowJob';

export const MainLayout: React.FC = () => {
  useEffect(() => { document.title = 'MIRAI — Workspace'; }, []);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.matchMedia('(max-width: 760px)').matches);

  return (
    <div style={{ display: 'flex', minHeight: '100vh', width: '100%' }}>
      {/* Persistent Left Sidebar */}
      <AppSidebar
        isCollapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
      />

      {/* Main Content Area */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <TopNavbar onToggleSidebar={() => setSidebarCollapsed(!sidebarCollapsed)} />

        <main className="workflow-main"
          style={{
            flex: 1,
            padding: '28px 32px',
            maxWidth: '1280px',
            width: '100%',
            margin: '0 auto',
          }}
        >
          <WorkflowJob />
          <Outlet />
        </main>
      </div>
    </div>
  );
};


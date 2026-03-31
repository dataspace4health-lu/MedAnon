import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from '@/context/AuthContext';
import { ConfigProvider } from '@/context/ConfigContext';
import { BulkExportProvider } from '@/context/BulkExportContext';
import { Toaster } from 'sonner';
import App from './App';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <ConfigProvider>
          <BulkExportProvider>
            <App />
            <Toaster position="bottom-right" />
          </BulkExportProvider>
        </ConfigProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>
);

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from '@/context/AuthContext';
import { ConfigProvider } from '@/context/ConfigContext';
import { Toaster } from 'sonner';
import App from './App';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <ConfigProvider>
          <App />
          <Toaster position="bottom-right" />
        </ConfigProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>
);

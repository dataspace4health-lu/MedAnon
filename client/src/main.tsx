import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider } from '@/context/AuthContext';
import { ConfigProvider } from '@/context/ConfigContext';
import { BulkExportProvider } from '@/context/BulkExportContext';
import { ThemeProvider } from '@/context/ThemeContext';
import { Toaster } from 'sonner';
import App from './App';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      // Retry transient failures with exponential backoff (capped at 30s).
      // Do not retry 4xx client errors — they will not succeed on retry.
      retry: (failureCount, error) => {
        const status = (error as { status?: number } | undefined)?.status;
        if (typeof status === 'number' && status >= 400 && status < 500) {
          return false;
        }
        return failureCount < 3;
      },
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 30_000),
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <BrowserRouter>
          <AuthProvider>
            <ConfigProvider>
              <BulkExportProvider>
                <App />
                <Toaster
                  position="bottom-right"
                  expand
                  richColors
                  closeButton
                  duration={4000}
                />
              </BulkExportProvider>
            </ConfigProvider>
          </AuthProvider>
        </BrowserRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>
);

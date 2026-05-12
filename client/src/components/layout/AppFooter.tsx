import nttDataLogo from '@/assets/ntt-data-logo.svg';

export function AppFooter() {
  const year = new Date().getFullYear();

  return (
    <footer className="border-t bg-background/95">
      <div className="mx-auto max-w-6xl px-4 py-1.5 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between">
          <img
            src={nttDataLogo}
            alt="NTT DATA"
            className="h-4 max-w-[60px] w-auto object-contain dark:invert"
          />
          <p className="text-[10px] text-muted-foreground">
            &copy; {year} NTT DATA. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  );
}

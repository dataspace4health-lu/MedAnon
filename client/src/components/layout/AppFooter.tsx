import ds4hLogo from '@/assets/ds4h-logo.png';

export function AppFooter() {
  const year = new Date().getFullYear();

  return (
    <footer className="border-t bg-background/95">
      <div className="mx-auto max-w-6xl px-4 py-2 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between">
          <img
            src={ds4hLogo}
            alt="Dataspace4Health"
            className="h-7 w-auto max-w-[200px] object-contain"
          />
          <p className="text-[10px] text-muted-foreground">
            &copy; {year} Dataspace4Health. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  );
}

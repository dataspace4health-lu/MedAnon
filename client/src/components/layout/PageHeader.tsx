export function PageHeader({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="mb-8 border-b pb-5">
      <h1 className="text-2xl font-bold tracking-tight text-foreground">
        {title}
      </h1>
      {description && (
        <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-muted-foreground">
          {description}
        </p>
      )}
    </div>
  );
}

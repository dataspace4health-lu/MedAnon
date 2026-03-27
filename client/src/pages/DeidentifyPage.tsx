import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { ArrowLeft, User } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DeidentifyPanel } from '@/components/shared/DeidentifyPanel';
import { useConfig } from '@/context/ConfigContext';

interface LocationState {
  name?: string;
  from?: string;
}

export default function DeidentifyPage() {
  const { patientId } = useParams<{ patientId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { configProfile } = useConfig();

  const state = (location.state ?? {}) as LocationState;
  const patientName = state.name ?? '';
  const from = state.from ?? -1;

  if (!patientId) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
        <p className="text-sm">No patient ID provided.</p>
        <Button variant="ghost" className="mt-4" onClick={() => navigate('/patients')}>
          Back to Patient Browser
        </Button>
      </div>
    );
  }

  return (
    <div>
      {/* Back navigation */}
      <div className="mb-6 flex items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate(from as number)}
          className="-ml-2 gap-1.5 text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back
        </Button>
        <span className="text-muted-foreground">/</span>
        <span className="text-sm text-muted-foreground">De-identify Patient</span>
      </div>

      {/* Patient identity header */}
      <div className="mb-6 flex items-center gap-3 rounded-xl border bg-card px-5 py-4 shadow-sm">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
          <User className="size-5" />
        </div>
        <div>
          <p className="font-semibold">{patientName || 'Unknown Patient'}</p>
          <p className="font-mono text-xs text-muted-foreground">{patientId}</p>
        </div>
        <div className="ml-auto">
          <span className="rounded-full border bg-muted/40 px-2.5 py-0.5 text-xs text-muted-foreground">
            Profile: <span className="font-medium text-foreground">{configProfile}</span>
          </span>
        </div>
      </div>

      {/* De-identification panel — full width, no sticky constraints */}
      <DeidentifyPanel
        patientId={patientId}
        patientName={patientName}
        configProfile={configProfile}
      />
    </div>
  );
}

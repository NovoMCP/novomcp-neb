"""
NEB Transition State Search — xTB binary via ASE Calculator.

Uses the xTB command-line binary (subprocess) wrapped in a custom ASE
Calculator. No tblite Python bindings needed — avoids the broken C
extension wheel issue entirely.

The subprocess overhead (~50ms per call) is acceptable for NEB because
each image evaluation does real QM work (~500ms-2s). Total overhead
for 8 images × 50 steps = ~20s out of ~200s total compute.
"""

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from ase import Atoms
from ase.calculators.calculator import Calculator, all_changes
from ase.mep.neb import NEB
from ase.optimize import BFGS

logger = logging.getLogger("novomcp-neb.engine")

XTB_BIN = shutil.which("xtb") or os.getenv("XTB_BIN", "xtb")
SCRATCH_DIR = Path(os.getenv("SCRATCH_DIR", "/app/scratch"))
HARTREE_TO_EV = 27.2114
EV_TO_KCAL = 23.0609


# =============================================================================
# ASE Calculator wrapping xTB binary
# =============================================================================

class XTBCalculator(Calculator):
    """ASE Calculator that calls xTB via subprocess.

    Each calculate() call writes an XYZ file, runs `xtb --sp --grad`,
    and parses the energy + gradient output.
    """
    implemented_properties = ["energy", "forces"]

    def __init__(self, charge=0, uhf=0, solvent=None, **kwargs):
        super().__init__(**kwargs)
        self.charge = charge
        self.uhf = uhf
        self.solvent = solvent

    def calculate(self, atoms=None, properties=None, system_changes=all_changes):
        super().calculate(atoms, properties, system_changes)

        workdir = tempfile.mkdtemp(dir=SCRATCH_DIR, prefix="xtb_neb_")
        try:
            # Write XYZ
            n = len(self.atoms)
            xyz_lines = [str(n), "NEB image"]
            for sym, pos in zip(self.atoms.get_chemical_symbols(), self.atoms.positions):
                xyz_lines.append(f"{sym} {pos[0]:.10f} {pos[1]:.10f} {pos[2]:.10f}")
            xyz_path = Path(workdir) / "input.xyz"
            xyz_path.write_text("\n".join(xyz_lines) + "\n")

            # Build command
            cmd = [XTB_BIN, str(xyz_path), "--sp", "--grad",
                   "--chrg", str(self.charge)]
            if self.uhf:
                cmd.extend(["--uhf", str(self.uhf)])
            if self.solvent:
                cmd.extend(["--alpb", self.solvent])

            import subprocess
            proc = subprocess.run(
                cmd, cwd=workdir,
                capture_output=True, text=True, timeout=120,
            )

            if proc.returncode != 0:
                err_msg = (proc.stderr or "").strip()
                out_msg = (proc.stdout or "").strip()
                logger.error(f"xTB failed (rc={proc.returncode}):\nSTDERR: {err_msg[-500:]}\nSTDOUT last 500: {out_msg[-500:]}")
                # SCF non-convergence on interpolated NEB images is common —
                # return a very high energy so NEB steers away from this geometry
                if "did not converge" in out_msg or "did not converge" in err_msg:
                    logger.warning("SCF did not converge — returning penalty energy for this image")
                    self.results = {
                        "energy": 1000.0,  # Very high energy in eV
                        "forces": np.zeros((len(self.atoms), 3)),
                    }
                    return
                raise RuntimeError(f"xtb failed: {err_msg[-500:]}")

            # Parse energy from stdout
            energy_hartree = None
            for line in proc.stdout.splitlines():
                if "TOTAL ENERGY" in line and "Eh" in line:
                    try:
                        energy_hartree = float(line.split()[-3])
                    except (ValueError, IndexError):
                        pass

            if energy_hartree is None:
                raise RuntimeError("Could not parse energy from xTB output")

            # Parse gradient from gradient file
            grad_file = Path(workdir) / "gradient"
            forces = np.zeros((n, 3))
            if grad_file.exists():
                grad_lines = grad_file.read_text().splitlines()
                # Format: after "cycle" header line, N lines of gradient values
                grad_start = None
                for i, line in enumerate(grad_lines):
                    if "cycle" in line:
                        grad_start = i + 1 + n  # skip coords, get gradients
                        break
                if grad_start is not None:
                    for j in range(n):
                        idx = grad_start + j
                        if idx < len(grad_lines):
                            parts = grad_lines[idx].split()
                            if len(parts) >= 3:
                                # Gradient in Hartree/Bohr → force = -gradient in eV/Å
                                gx = float(parts[0].replace("D", "E"))
                                gy = float(parts[1].replace("D", "E"))
                                gz = float(parts[2].replace("D", "E"))
                                # Hartree/Bohr → eV/Å: multiply by -HARTREE_TO_EV / 0.529177
                                conv = -HARTREE_TO_EV / 0.529177
                                forces[j] = [gx * conv, gy * conv, gz * conv]

            self.results = {
                "energy": energy_hartree * HARTREE_TO_EV,  # eV
                "forces": forces,  # eV/Å
            }

        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# =============================================================================
# Result dataclass
# =============================================================================

@dataclass
class NEBResult:
    success: bool
    activation_energy_kcal: Optional[float] = None
    activation_energy_ev: Optional[float] = None
    reverse_barrier_kcal: Optional[float] = None
    reverse_barrier_ev: Optional[float] = None
    ts_energy_ev: Optional[float] = None
    reactant_energy_ev: Optional[float] = None
    product_energy_ev: Optional[float] = None
    ts_geometry_xyz: Optional[str] = None
    mep_energies_ev: Optional[list[float]] = None
    mep_energies_kcal: Optional[list[float]] = None
    n_images: int = 0
    converged: bool = False
    n_steps: int = 0
    method: str = "GFN2-xTB CI-NEB"
    wall_time_seconds: Optional[float] = None
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


# =============================================================================
# Helpers
# =============================================================================

def _parse_xyz_string(xyz_str):
    lines = xyz_str.strip().splitlines()
    n = int(lines[0].strip())
    symbols = []
    positions = []
    for line in lines[2:2 + n]:
        parts = line.split()
        symbols.append(parts[0])
        positions.append([float(parts[1]), float(parts[2]), float(parts[3])])
    return np.array(positions), symbols


def _atoms_to_xyz(atoms):
    n = len(atoms)
    e = atoms.get_potential_energy() if atoms.calc else 0.0
    lines = [str(n), f"Energy: {e:.6f} eV"]
    for sym, pos in zip(atoms.get_chemical_symbols(), atoms.positions):
        lines.append(f"{sym:2s} {pos[0]:14.8f} {pos[1]:14.8f} {pos[2]:14.8f}")
    return "\n".join(lines) + "\n"


# =============================================================================
# Main NEB function
# =============================================================================

def is_available():
    return shutil.which(XTB_BIN) is not None


def run_neb(
    reactant_xyz, product_xyz,
    n_images=8, charge=0, uhf=0, solvent=None,
    fmax=0.05, max_steps=200, climb=True,
):
    t0 = time.time()
    warnings = []

    if not is_available():
        return NEBResult(success=False, error="xTB binary not found")

    try:
        r_pos, r_sym = _parse_xyz_string(reactant_xyz)
        p_pos, p_sym = _parse_xyz_string(product_xyz)

        if r_sym != p_sym:
            return NEBResult(success=False,
                error=f"Reactant and product have different atoms: {r_sym} vs {p_sym}")

        calc_kwargs = {"charge": charge, "uhf": uhf, "solvent": solvent}

        reactant = Atoms(symbols=r_sym, positions=r_pos)
        reactant.calc = XTBCalculator(**calc_kwargs)

        product = Atoms(symbols=p_sym, positions=p_pos)
        product.calc = XTBCalculator(**calc_kwargs)

        e_reactant = reactant.get_potential_energy()
        e_product = product.get_potential_energy()

        images = [reactant]
        for _ in range(n_images):
            img = reactant.copy()
            img.calc = XTBCalculator(**calc_kwargs)
            images.append(img)
        images.append(product)

        neb = NEB(images, climb=climb, k=0.1)
        neb.interpolate()

        opt = BFGS(neb, logfile=None)
        converged = opt.run(fmax=fmax, steps=max_steps)
        n_steps = opt.nsteps

        mep_ev = [float(img.get_potential_energy()) for img in images]
        mep_kcal = [e * EV_TO_KCAL for e in mep_ev]

        ts_idx = int(np.argmax(mep_ev))
        ts_energy = mep_ev[ts_idx]

        forward = ts_energy - e_reactant
        reverse = ts_energy - e_product

        ts_xyz = _atoms_to_xyz(images[ts_idx])
        wall = round(time.time() - t0, 1)

        if not converged:
            warnings.append(f"NEB did not converge in {max_steps} steps")

        return NEBResult(
            success=True,
            activation_energy_kcal=round(forward * EV_TO_KCAL, 3),
            activation_energy_ev=round(forward, 4),
            reverse_barrier_kcal=round(reverse * EV_TO_KCAL, 3),
            reverse_barrier_ev=round(reverse, 4),
            ts_energy_ev=round(ts_energy, 6),
            reactant_energy_ev=round(e_reactant, 6),
            product_energy_ev=round(e_product, 6),
            ts_geometry_xyz=ts_xyz,
            mep_energies_ev=[round(e, 6) for e in mep_ev],
            mep_energies_kcal=[round(e, 3) for e in mep_kcal],
            n_images=n_images + 2,
            converged=bool(converged),
            n_steps=n_steps,
            method=f"GFN2-xTB {'CI-' if climb else ''}NEB (subprocess)",
            wall_time_seconds=wall,
            warnings=warnings,
        )
    except Exception as e:
        return NEBResult(success=False, error=f"NEB failed: {str(e)}",
                        wall_time_seconds=round(time.time() - t0, 1))

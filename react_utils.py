import os
import shutil
import subprocess
import numpy as np
import tempfile
import re
from io_utils import traj2str

# ------------------- utility routines -----------------------------------#
def dump_succ_opt(output_folder, structures, energies,
                  split=False):
    
    os.makedirs(output_folder, exist_ok=True)
    # Dump the optimized structures in one file                
    with open(output_folder + "/opt.xyz", "w") as f:
        for s in structures:
            f.write(s)
            
    if split:
        # Also dump the optimized structures in many files            
        for stepi, s in enumerate(structures):
            with open(output_folder + "/opt%4.4i.xyz" % stepi, "w") as f:
                f.write(s)

def quick_opt_job(xtb, xyz, level, xcontrol):
    # TODO comment
    with tempfile.NamedTemporaryFile(suffix=".xyz",
                                     dir=xtb.scratchdir) as T:
        T.write(bytes(xyz, 'ascii'))
        T.flush()
        opt = xtb.optimize(T.name,
                           T.name,
                           level=level,
                           xcontrol=xcontrol)
        opt()
        xyz, E = traj2str(T.name, 0)
    return xyz, E

# ------------------------------------------------------------------------------#
def stretch(xtb, initial_xyz,
            atom1, atom2, low, high, npts,
            parameters,
            failout=None,
            verbose=True):
    """Optimize a structure through successive constraints.

    TODO FIX

    Parameters:
    -----------

    xtb (xtb_driver) : driver object for xtb.

    initial_xyz (str): path to initial structure xyz file.

    parameters (dict) : additional parameters, as obtained from
    default_parameters() above. TODO: Describe parameters in more details

    Optional Parameters:
    --------------------

    failout (str) : Path where logging information is output if the
    optimization fails.

    verbose (bool) : print information about the run. defaults to True.

    Returns:
    --------

    structures : list of .xyz formatted strings that include every structure
    through the multiple optimization.

    energies : list of floats of xtb energies (in Hartrees) for the structures.

    """
    # Make scratch files
    fdc, current = tempfile.mkstemp(suffix=".xyz", dir=xtb.scratchdir)

    # prepare the current file
    shutil.copyfile(initial_xyz, current)

    opt = xtb.optimize(current,
                       current,
                       failout=failout,
                       level=parameters["optim"],
                       xcontrol=dict(
                           wall=parameters["wall"],
                           constrain=(
                               "force constant=%f" % parameters["force"],
                               "distance: %i, %i, %f" % (atom1, atom2, low)),
                           scan=("1: %f, %f, %i" % (low, high, npts),)
                       ))

    error = opt()
    structs, energies = traj2str(current)
            
    if verbose:
        for k, E in enumerate(energies):
            print("   👣=%4i    energy💡= %9.5f Eₕ"%(k, E))
                                                     
    # Got to make sure that you close the filehandles!
    os.close(fdc)
    os.remove(current)
    return structs, energies
    
def successive_optimization(xtb,
                            initial_xyz,
                            constraints,
                            parameters,
                            barrier=True,
                            failout=None,
                            verbose=True):
    """Optimize a structure through successive constraints.

    Takes an initial structure and successively optimize it applying the
    sequence of $constrain objects in constraints. These can be generated by
    code such as this one, which stretch a bond between atoms 10 and 11 in
    initial_xyz from 1.06 A to 3 x 1.06 A in 80 steps,

    stretch_factors = np.linspace(1.0, 3.0, 80)
    constraints = [("force constant = 0.5",
                    "distance: %i, %i, %f" % (10, 11, stretch * 1.06)
                   for stretch in stretch_factors]

    Parameters:
    -----------

    xtb (xtb_driver) : driver object for xtb.

    initial_xyz (str): path to initial structure xyz file.

    constraints (list): list of constraints. Each constraints should be a
    tuple of parameters to be put into a $constrain group in xcontrol.

    parameters (dict) : additional parameters, as obtained from
    default_parameters() above. TODO: Describe parameters in more details

    Optional Parameters:
    --------------------

    failout (str) : Path where logging information is output if the
    optimization fails.

    verbose (bool) : print information about the run. defaults to True.

    Returns:
    --------

    structures : list of .xyz formatted strings that include every structure
    through the multiple optimization.

    energies : list of floats of xtb energies (in Hartrees) for the structures.

    opt_indices : list of integers representing the indices of the optimized
    structures at each step.

    """
    if not constraints:
        # nothing to do
        return [], []

    # Make scratch files
    fdc, current = tempfile.mkstemp(suffix=".xyz", dir=xtb.scratchdir)
    fdl, log = tempfile.mkstemp(suffix="_log.xyz", dir=xtb.scratchdir)
    fdr, restart = tempfile.mkstemp(dir=xtb.scratchdir)

    # prepare the current file
    shutil.copyfile(initial_xyz, current)
    structures = []
    energies = []

    if verbose:
        print("  %i successive optimizations" % len(constraints))

    for i in range(len(constraints)):
        direction = "->"
        if verbose:
            print("      " + direction + "  %3i " % i, end="")
            
        opt = xtb.optimize(current,
                           current,
                           log=log,
                           failout=failout,
                           level=parameters["optim"],
                           restart=restart,
                           xcontrol=dict(
                               wall=parameters["wall"],
                               constrain=constraints[i]))
        error = opt()
        if error != 0:
            # An optimization failed, we get out of this loop.
            break
        
        news, newe = traj2str(log)
        structures += [news[np.argmin(newe)]]
        energies += [np.min(newe)]
            
        if verbose:
            print("   step👣=%4i    energy💡= %9.5f Eₕ"%(len(news),
                                                         energies[-1]))

        if newe[-1] > parameters["emax"] and barrier:
            if verbose:
                print("   ----- energy threshold exceeded -----")
            break

    # Got to make sure that you close the filehandles!
    os.close(fdc)
    os.close(fdl)
    os.close(fdr)
    os.remove(current)
    os.remove(log)
    os.remove(restart)
    return structures, energies
        

def metadynamics_jobs(xtb,
               mtd_index,
               input_folder,
               output_folder,
               constraints,
               parameters):
    """Return a metadynamics search job for other "transition" conformers.

    mtd_index is the index of the starting structure to use as a starting
    point in the metadynamics run. Returns an unevaluated xtb_job to be used
    in ThreadPool.

    Parameters:
    -----------

    xtb (xtb_driver) : driver object for xtb.

    mtd_index (int) : index of the starting structure, as generated from
    generate_starting_structures. Correspond to the index of the element in
    the constratins list for which metadynamics is run.

    input_folder (str) : folder containing the MTD input structures.

    output_folder (str) : folder where results are stored.

    constraints (list): list of constraints. Each constraints should be a
    tuple of parameters to be put into a $constrain group in xcontrol.

    parameters (dict) : additional parameters, as obtained from
    default_parameters() above. TODO: Describe parameters in more details

    Optional Parameters:
    --------------------

    verbose (bool) : print information about the run. defaults to True.

    Returns:
    --------

    None

    """
    os.makedirs(output_folder, exist_ok=True)

    mjobs = []
    meta = parameters["metadynamics"]
    inp = input_folder + "/opt%4.4i.xyz" % mtd_index
    # Set the time of the propagation based on the number of atoms.
    with open(inp, "r") as f:
        Natoms = int(f.readline())
    md = meta["md"] + ["time=%f" % (meta["time_per_atom"] * Natoms),
                       "dump=%f" % (meta["time_per_atom"] * Natoms
                                    * 1000.0/meta["nmtd"])]
    S = "save=%i" % meta["save"]
        
    for metadyn_job, metadyn_params in enumerate(meta["jobs"]):
        outp = output_folder + "/mtd%4.4i_%2.2i.xyz" % (mtd_index,metadyn_job)
        mjobs += [
            xtb.metadyn(inp, outp,
                        failout=output_folder +
                        "/FAIL%4.4i_%2.2i.xyz" % (mtd_index, metadyn_job),
                        xcontrol=dict(
                            wall=parameters["wall"],
                            metadyn=metadyn_params + [S],
                            md=md,
                            constrain=constraints[mtd_index]))]
    return mjobs

    
def reaction_job(xtb,
                 initial_xyz,
                 mtd_index,
                 output_folder,
                 constraints,
                 parameters):
    """Take structures generated by metadynamics and build a reaction trajectory.

    Takes a structure generated by metadynamics_search() and successively
    optimize it by applying the sequence of $constrain objects in constraints
    forward from mtd_index to obtain products, and backward from mtd_index to
    obtain reactants.

    This is the final step in the reaction space search, and it generates
    molecular trajectories. It should be noted that this function returns an
    unevaluated job, to be fed to ThreadPool.

    Parameters:
    -----------

    xtb (xtb_driver) : driver object for xtb.

    initial_xyz (str) : initial xyz as a string.

    mtd_index (int) : Index of the element in the constraints list that
    generated the starting structures.

    output_folder (str) : folder where results are stored.

    constraints (list): list of constraints. Each constraints should be a
    tuple of parameters to be put into a $constrain group in xcontrol.

    parameters (dict) : additional parameters, as obtained from
    default_parameters() above. TODO: Describe parameters in more details

    Optional Parameters:
    --------------------

    verbose (bool) : print information about the run. defaults to True.

    Returns:
    --------

    react_job() : function which, when evaluated, computes the trajectory

    """

    def react_job():
        os.makedirs(output_folder, exist_ok=True)

        with open(output_folder + "/initial.xyz", "w") as f:
            f.write(initial_xyz)

        # Forward reaction
        fstructs, fe = successive_optimization(
            xtb, output_folder + "/initial.xyz",
            constraints[mtd_index:],
            parameters,
            failout=output_folder + "/FAILED_FORWARD",
            verbose=False)          # otherwise its way too verbose

        if len(fstructs) == 0:
            # fail on the first opt
            return

        # fstructs 0 is already optimized so we use it as the starting point
        # for the backward propagation
        with open(output_folder + "/initial_backward.xyz", "w") as f:
            f.write(fstructs[0])
        
        # Backward reaction
        bstructs, be = successive_optimization(
            xtb, output_folder + "/initial_backward.xyz",
            constraints[:mtd_index][::-1],
            parameters,
            failout=output_folder + "/FAILED_BACKWARD",            
            verbose=False)          # otherwise its way too verbose

        # Dump forward reaction and backward reaction quantities
        dump_succ_opt(output_folder,
                      bstructs[::-1] + fstructs,
                      be[::-1] + fe,
                      split=False)

    return react_job





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

def stretch_constraint(atom1, atom2, val, force):
    return ("force constant=%f" % force,
            "distance: %i, %i, %f" % (atom1, atom2, val))

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
                           constrain=stretch_constraint(atom1, atom2,
                                                        low, parameters["force"]),
                           scan=("1: %f, %f, %i" % (low, high, npts),)))

    error = opt()
    structs, energies = traj2str(current)
            
    if verbose:
        for k, E in enumerate(energies):
            print("   👣=%4i    energy💡= %9.5f Eₕ"%(k, E))
                                                     
    # Got to make sure that you close the filehandles!
    os.close(fdc)
    os.remove(current)
    return structs, energies
    
def metadynamics_jobs(xtb,
                      mtd_index,
                      atom1, atom2, low, high, npts,
                      input_folder,
                      output_folder,
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
    
    # stretch points
    points = np.linspace(low, high, npts)
    
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
                            cma="",
                            constrain=stretch_constraint(atom1, atom2,
                                                         points[mtd_index],
                                                         parameters["force"])))]
    return mjobs

    
def reaction_job(xtb,
                 initial_xyz,
                 mtd_index,
                 atom1, atom2, low, high, npts,                 
                 output_folder,
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

    TODO

    output_folder (str) : folder where results are stored.

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

        points = np.linspace(low, high, npts)
        forw = points[mtd_index:]
        back = points[:mtd_index][::-1]

        # Forward reaction
        fstructs, fe = stretch(
            xtb, output_folder + "/initial.xyz",
            atom1, atom2, forw[0], forw[-1], len(forw),
            parameters,
            failout=output_folder + "/FAILED_FORWARD",
            verbose=False)          # otherwise its way too verbose

        # fstructs 0 is already optimized so we use it as the starting point
        # for the backward propagation
        with open(output_folder + "/initial_backward.xyz", "w") as f:
            f.write(fstructs[0])
        
        # Backward reaction
        if len(back):
            bstructs, be = stretch(
                xtb, output_folder + "/initial_backward.xyz",
                atom1, atom2, back[0], back[-1], len(back),            
                parameters,
                failout=output_folder + "/FAILED_BACKWARD",            
                verbose=False)          # otherwise its way too verbose
        else:
            bstructs = []
            be = []

        # Dump forward reaction and backward reaction quantities
        dump_succ_opt(output_folder,
                      bstructs[::-1] + fstructs,
                      be[::-1] + fe,
                      split=False)

    return react_job





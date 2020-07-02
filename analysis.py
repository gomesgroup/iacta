import io_utils
import numpy as np
from constants import hartree_ev, ev_kcalmol
import pandas as pd
import glob
import json
import os

def postprocess_reaction(xtb, react_folder, metadata={}):
    """Extract chemical quantities from a reaction trajectory

    Parameters:
    -----------
    xtb (xtb_utils.xtb_driver instance): xtb driver for optimizing products.

    react_folder (str) : folder storing the reaction data, obtained from the
    reaction_job() routine in react.py.

    Returns:
    -------
    A dictionary that describes the trajectory in react_folder

    """
    opt = react_folder + "/opt.xyz"
    # Get the smiles
    # TODO: waste of computer time reloading the same file three times...
    smiles, E = io_utils.traj2smiles(opt, chiral=True)
    smiles_iso, _ = io_utils.traj2smiles(opt, chiral=False)
    structs, _ = io_utils.traj2str(opt)

    mols = [smiles[0]]
    regions = []
    rstart = 0

    # loop through smiles and detect changes in smiles
    for si,s in enumerate(smiles):
        if s == mols[-1]:
            pass
        else:
            mols += [s]
            regions += [(rstart,si)]
            rstart = si

    regions += [(rstart, len(smiles))]

    imins = []
    for start,end in regions:
        imin = np.argmin(E[start:end]) + start
        stable = True
        # Check if imin is a local minima
        if imin < len(smiles)-1:
            if E[imin]>E[imin+1]:
                stable = False
        if imin > 0:
            if E[imin]>E[imin-1]:
                stable = False

        if stable:
            imins += [imin]

    # Important potential energy surface points
    ipots = [imins[0]]
    # determine if these are stable, ts or errors
    stable = [True]
    for k in range(1,len(imins)):
        # maximum between minima
        imax = np.argmax(E[imins[k-1]:imins[k]]) + imins[k-1]
        # if imax is different from both, we add it to the pot curve too
        if imax != imins[k] and imax != imins[k-1]:
            ipots += [imax]
            stable += [False]
        ipots += [imins[k]]
        stable += [True]

    # Optimize "stable" structures
    chiral_smiles = []
    isomeric_smiles = []
    energies = []
    for i, sindex in enumerate(ipots):
        if stable[i]:
            fn = react_folder + "/stable_%4.4i.xyz" % sindex
            with open(fn, "w") as f:
                f.write(structs[sindex])

            # optimize the structure without constraints
            xtb.optimize(fn, fn,
                         level="vtight")

            # Read back
            # TODO:again, reloading the same file twice
            s, eprod = io_utils.traj2smiles(fn, chiral=True, index=0)
            chiral_smiles += [s]
            s, _ = io_utils.traj2smiles(fn, chiral=False, index=0)
            isomeric_smiles += [s]
            energies += [float(eprod)]
        else:
            fn = react_folder + "/ts_%4.4i.xyz" % sindex
            with open(fn, "w") as f:
                f.write(structs[sindex])

            chiral_smiles += [smiles[sindex]]
            isomeric_smiles += [smiles_iso[sindex]]
            energies +=[float(E[sindex])]

    out = {
        "E":energies,
        "SMILES_c":chiral_smiles,
        "SMILES_i":isomeric_smiles,
        "is_stable":stable,
        "folder":react_folder,
        # type conversions for json-izability
        "stretch_points":[int(i) for i in ipots]}

    for key,val in metadata.items():
        out[key] = val

    with open(react_folder + "/reaction_data.json", "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)

    return out

def read_all_reactions(output_folder,
                       verbose=True,
                       restart=True,
                       reparser=None,
                       save=True):
    """Read and parse all reactions in a given folder."""
    # strip last / for nicer output
    if output_folder[-1] == '/':
        output_folder = output_folder[:-1]

    folders = glob.glob(output_folder + "/reactions/[0-9]*")
    if verbose:
        print("Parsing folder <%s>, with" % output_folder)
        print("   %6i trajectories..." % len(folders))

    failed = []
    pathways = []
    if restart:
        try:
            old_df = pd.read_pickle(output_folder+"/results_raw.pkl")
        except FileNotFoundError:
            old_df = pd.DataFrame()
        else:
            if verbose:
                print(" - %6i trajectories in restart file" % len(old_df))
    else:
        old_df = pd.DataFrame()

    old_indices = old_df.index
    new_indices = []
    for f in folders:
        # nasty parsing...
        if f in old_indices:
            # already in restart file
            continue
        try:
            if os.path.exists(f + "/FAILED_FORWARD")\
               or os.path.exists(f + "/FAILED_BACKWARD"):
                raise OSError()

            fn = f + "/reaction_data.json"
            with open(fn,"r") as fin:
                read_out = json.load(fin)

            # Overwrite whatever folder is in the json file for the current
            # one. This is useful if the folder has changed name.
            read_out['folder'] = f
        except:
            # Convergence failed
            failed += [f]
        else:
            new_indices += [f]
            pathways += [read_out]

            if reparser is None:
                # Do not reparse structure
                pass
            else:
                # Do reparse based on the xyz files.
                new_ids = []
                for k,index in enumerate(read_out['stretch_points']):
                    if read_out["is_stable"][k]:
                        pref = "stable"
                    else:
                        pref = "ts"

                    # Reparse the xyz files for stable states and TSs
                    new_ids += [reparser(f + "/" + pref +  "_%4.4i.xyz" % index)]

                read_out['reparsed'] = new_ids


    if verbose:
        print(" - %6i that did not converge" % len(failed))
        print("--------------")

    new = pd.DataFrame(pathways, index=new_indices)
    data = old_df.append(new)

    if verbose:
        if len(old_df):
            print(" = %6i new pathways loaded" % len(pathways))
            print("   %6i pathways including restart" % len(data))
        else:
            print(" = %6i pathways" % len(pathways))

    if save:
        if len(new):
            if verbose:
                print("       ... saving new data ...")
            data.to_pickle(output_folder+"/results_raw.pkl")

    if verbose:
        print("                                      done.")

    return data

def get_species_table(pathways, verbose=True, chemical_id="SMILES_i"):
    if verbose:
        print("\nBuilding table of chemical species, from")
        print("   %6i reaction pathways" % len(pathways))

    species = {}

    for irow, row in pathways.iterrows():
        for k in range(len(row.is_stable)):
            if row.is_stable[k]:
                smi = row[chemical_id][k]
                if smi in species:
                    if row.E[k] < species[smi]['E']:
                        species[smi] = {
                            'smiles':smi,
                            'E':row.E[k],
                            'file':row.folder + "stable_%4.4i.xyz" % row.stretch_points[k],
                            'position':row.stretch_points[k]}
                else:
                    species[smi] = {
                        'smiles':smi,
                        'E':row.E[k],
                        'file':row.folder + "stable_%4.4i.xyz" % row.stretch_points[k],
                        'position':row.stretch_points[k]}

    if len(species) == 0:
        print("\nNo chemical species identified!")
        raise SystemExit(0)
    k,v = zip(*species.items())
    out = pd.DataFrame(v).sort_values('E').reset_index(drop=True).set_index('smiles')
    if verbose:
        print("   %6i stable-ish species found" % len(out))
        evspan = (out.E.max() - out.E.min()) * hartree_ev
        print("          with energies spanning %3.1f eV" % (evspan))
        print("          saving structures...")
        print("                                      done.")

    return out

def reaction_network_layer(pathways, reactant, species,
                           exclude=[], chemical_id="SMILES_i"):
    to_smiles = []
    ts_i = []
    prod_i = []
    react_i = []
    ts_E = []
    barrier = []
    folder = []
    mtdi = []
    dE = []
    local_dE = []
    Ereactant = species.E.loc[reactant]

    for k, rowk in pathways.iterrows():
        smiles = rowk[chemical_id]
        if reactant in smiles:
            i = smiles.index(reactant)
            E = rowk.E
            stable = rowk.is_stable

            # forward
            for j in range(i+1, len(stable)):
                if stable[j] and smiles[j] not in exclude:
                    # we have a stable -> stable reaction

                    # Get transition state
                    tspos = np.argmax(E[i:j]) + i
                    ts = E[tspos]

                    # TODO: Major issue. Some trajectories are completely
                    # messed up and don't have a barrier at all. We get rid of
                    # these artifically here.
                    if ts <= E[i] or ts <= E[j]:
                        continue

                    ts_E += [ts]
                    product = smiles[j]
                    Eproducts = species.E.loc[product]
                    to_smiles += [product]
                    dE += [Eproducts - Ereactant]
                    local_dE += [E[j] - E[i]]
                    ts_i += [rowk.stretch_points[tspos]]
                    react_i += [rowk.stretch_points[i]]
                    prod_i += [rowk.stretch_points[j]]

                    folder += [rowk.folder]
                    mtdi += [rowk.mtdi]
                    barrier += [E[tspos]-E[i]]

            for j in range(i-1, -1, -1):
                if stable[j] and smiles[j] not in exclude:
                    # do the same but in the other direction

                    # Get transition state
                    tspos = np.argmax(E[j:i]) + j
                    ts = E[tspos]

                    # TODO: Major issue. Some trajectories are completely
                    # messed up and don't have a barrier at all. We get rid of
                    # these artifically here.
                    if ts <= E[i] or ts <= E[j]:
                        continue

                    ts_E += [ts]
                    product = smiles[j]
                    Eproducts = species.E.loc[product]
                    to_smiles += [product]
                    dE += [Eproducts - Ereactant]
                    local_dE += [E[j] - E[i]]
                    ts_i += [rowk.stretch_points[tspos]]
                    react_i += [rowk.stretch_points[i]]
                    prod_i += [rowk.stretch_points[j]]

                    folder += [rowk.folder]
                    mtdi += [rowk.mtdi]
                    barrier += [E[tspos]-E[i]]

    out = pd.DataFrame({
        'from':[reactant] * len(to_smiles),
        'to':to_smiles,
        'E_TS':ts_E,
        'barrier':barrier,
        'dE':dE,
        'local_dE':local_dE,
        'i_TS':ts_i,
        'i_P':prod_i,
        'i_R':react_i,
        'folder':folder,
        'mtdi':mtdi})


    return out


def analyse_reaction_network(pathways, species, reactants, verbose=True,
                             sort_by_barrier=False, reaction_local=False,
                             chemical_id="SMILES_i"):
    final_reactions = []             # container for reduced quantities
    reactions_table = pd.DataFrame() # all reactions

    verbose=True
    if verbose:
        print("\nReaction network analysis")

    todo = reactants[:]
    done = []

    layerind = 1
    iden = 1
    while todo:
        current = todo.pop(0)
        layer = reaction_network_layer(pathways, current, species,
                                       exclude=done + [current],
                                       chemical_id=chemical_id)
        E0 = species.loc[current].E
        products = list(set(layer.to))
        if sort_by_barrier:
            if reaction_local:
                products = sorted(products,
                                  key=lambda x:layer[layer.to==x].barrier.min())
            else:
                products = sorted(products,
                                  key=lambda x:layer[layer.to==x].E_TS.min())
        else:
            if reaction_local:
                products = sorted(products,
                                  key=lambda x:layer[layer.to==x].local_dE.min())
            else:
                products = sorted(products,
                                  key=lambda x:layer[layer.to==x].dE.min())

        if len(products) == 0:
            done += [current]
            continue

        if verbose:
            print("-"*78)
            print("%i." % layerind)
            print("  %s" % current)

        for p in products:
            if verbose:
                print("  → %s" % p)
            reacts = layer[layer.to == p]

            if reaction_local:
                best = reacts.loc[reacts.barrier.idxmin()]
                TS = best.barrier * hartree_ev * ev_kcalmol
                dE = best.local_dE * hartree_ev * ev_kcalmol
            else:
                best = reacts.loc[reacts.E_TS.idxmin()]
                TS = (best.E_TS - E0) * hartree_ev * ev_kcalmol
                dE = best.dE * hartree_ev * ev_kcalmol

            new_reacts = reacts.drop(
                columns=['from', 'to', 'dE', 'local_dE']
            ).sort_values('E_TS').reset_index(drop=True)
            new_reacts['reaction_id'] = iden
            new_reacts['traj_id'] = new_reacts.index + 1
            reactions_table = reactions_table.append(new_reacts)


            if verbose:
                print("       ΔReaction #%i" % iden)
                print("       ΔE(R->P)  = %8.4f kcal/mol" % dE)
                print("       ΔE(R->TS) = %8.4f kcal/mol" % TS)
                print("           %s" % best.folder)
                print("           + %5i similar pathways\n" % (len(reacts)-1))

            final_reactions += [
                {'from':current, 'to':p,
                 'reaction_id':iden,
                 'dE':dE,
                 'dE_TS':TS,
                 'best_TS_pathway':best.folder,
                 'TS_index':best.i_TS,
                 'mtdi':best.mtdi,
                }
            ]
            iden += 1

            if (not p in done) and (not p in todo):
                todo += [p]

        done += [current]
        layerind += 1

    reactions_table = reactions_table.set_index(['reaction_id', 'traj_id'])
    final_reactions = pd.DataFrame(final_reactions).set_index('reaction_id')
    return final_reactions, reactions_table

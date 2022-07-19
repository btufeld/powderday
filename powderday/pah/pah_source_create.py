import numpy as np
from powderday.pah.pah_file_read import read_draine_file
from powderday.helpers import find_nearest
from astropy import units as u
from astropy import constants as constants
import powderday.config as cfg
import pdb
from tqdm import tqdm
from powderday.pah.isrf_decompose import get_beta_nnls
import os,glob
from multiprocessing import Pool
from functools import partial
from datetime import datetime
#notes - 
#ncells are at np.arange(100) - fix this

#see if we can get ocmpute_Grid_pah_lumionsity working first without starmp

#then follow preraks example for how to get it in starmp - looks easy enough (i hope!)


 #FIXED -- WAS ZEROS IN THE LUMINOSITY ARRAY CAUSING NANS 1. there are bugs here -- when we run on 1500 sources we either get
#NaNs in the final flux array, or seg faults.  it's not clear why.
#putting in a pd.bset_trace() and looking at m.sources (right before
#SED propagation in front end) to comppare the units and ranges of the
#different types of sources may help to see if there's a units issue,
#or a wavelength range issue.

#NEEDED IF SEG FAULTS OCCUR 1a. beyond units check if a point source collection saves us (maybe
#it's a memory thing given the number of source?)

#1b. maybe we only want to include sources above some percentile in
#luminosity?  this could be possible given the large number of sources
#that it takes to seg fault (1500+).

#DONE 1c. maybe carefully compare against how the individual point sources
#are added with AGN to see how this compares -- maybe there's
#somewhere that i'm making a mistake.

#THIS IS THE BASELINE ISSUE 1d. is it okay that there are pah fluxes that are 0 when considering the entire SED? should we make them a min value

#2. once this is sorted, need to:

#2a0 - pool.map so it's not crazy slow

#2a. have the code know if its neutral or ion and use that luminosity

#2b. manually add the logU>4 sources

#2c. only compute this for PAHs and not all grains (or even all graphites)

def compute_grid_PAH_luminosity(cell_list,beta_nnls,grid_of_sizes,numgrains,draine_sizes,draine_lam,neutral_PAH_reference_objects,draine_bins_idx):

    for cell in cell_list:
        print(cell)
        beta_cell = beta_nnls[:,cell]
        beta_cell = beta_cell/np.max(beta_cell)
        
        #need to make a temporary (for this cell) PAH_list that is
        #just n_draine_sizes long that is convolved with beta_nnls
        pah_grid = np.zeros([len(draine_sizes),len(draine_lam)])
        for j in range(len(beta_cell)):
            PAH_list = neutral_PAH_reference_objects[j]
            temp_pah_grid = np.array([x.lum for x in PAH_list])
            temp_pah_grid *= beta_cell[j]
            pah_grid += temp_pah_grid #this is the running summation of the (n_sizes,n_lam) pah grid for the i-th cell
            
            
        #set the PAH luminosity of the cell to be the dot product of
        #the Draine luminosities (i.e., pah_grid[draine_bins_idx,:] which has
        #dimensions (simulation_sizes,wavelengths)) with the actual
        #grain size distribution in that cell (i.e.,
        #grid_of_sizes[i_cell,:]). note, we take the transpose of
        #grid_of_sizes to get the dimensions to match up correctly for the dot product
        grid_PAH_luminosity = np.dot(pah_grid[draine_bins_idx,:].T, grid_of_sizes.T[:,cell])
        particle_PAH_luminosity = np.dot(pah_grid[draine_bins_idx,:].T,numgrains.T[:,cell])
    
    return grid_PAH_luminosity,particle_PAH_luminosity



def pah_source_add(ds,reg,m,boost):
    
    LUM_FLOOR = 1.e20 #efg/s -- just some small value compared to the ~few Lsun we typically get in a cell

    
    #first - establish where we're working
    draine_directories = []
    print('powderday/pah/pah_source_create]: reading from the following Draine PAH directories')
    for it in os.scandir(cfg.par.draine_data_dir):
        if it.is_dir():
            print(it.path)
            draine_directories.append(it.path)


    #first establish the grain size distribution and sizes from the
    #hydro simulation
    grid_of_sizes = ds.parameters['reg_grid_of_sizes']

    simulation_sizes = (ds.parameters['grain_sizes_in_micron']*u.micron)


    #determine q_PAH for analysis and save it to parameters for
    #writing out DEBUG - WE SHOULD CHANGE THIS TO INCLUDE A FEW
    #DIFFRENT POSSIBILITIES, IUNCLUDING (A) COMPUTING QPAH AS IS, AND
    #(B) COMPUTING QPAH DIRECTLY FROM THE SIMULATION IN THE POSSIBLE
    #CASE THAT IT EXPLICITLY MODELS AROMATIC GRAPHITES

    ad = ds.all_data()

    idx_pah = np.where(simulation_sizes.to(u.cm).value <= 3.e-7)[0]


    dN_pah = np.sum(reg['particle_dust','numgrains'][:,idx_pah],axis=1)
    dN_total = np.sum(reg['particle_dust','numgrains'],axis=1)

    q_pah = (dN_pah * reg['particle_dust','mass'])/(dN_total*reg['particle_dust','mass'])
    q_pah = q_pah * reg['particle_dust','carbon_fraction']

    reg.parameters['q_pah'] = q_pah
    

    #compute the mass weighted grain size distributions for comparison in analytics.py 
    #try: #for mesh based code
    #particle_mass_weighted_gsd = np.average(reg['dust','numgrains'],weights=reg['dust','mass'],axis=0)
    #grid_mass_weighted_gsd = np.average(grid_of_sizes,weights=reg['dust','mass'],axis=0)
    #except:
    particle_mass_weighted_gsd = np.average(reg['particle_dust','numgrains'],weights=reg['dust','mass'],axis=0)
    try: #for octree    
        grid_mass_weighted_gsd = np.average(grid_of_sizes,weights=reg['dust','smoothedmasses'],axis=0)
    except:
        grid_mass_weighted_gsd = np.average(grid_of_sizes,weights=reg['dust','mass'],axis=0)

    #second, read the information from the Draine files. We can do
    #this just for an arbitrary file in one of the draine_directories
    #since the size bins and wavelengths are all the same.
    temp_filename = glob.glob(draine_directories[0]+'/*iout_graD16*nb*_0.00')[0]
    temp_PAH_list = read_draine_file(temp_filename)
    draine_sizes = temp_PAH_list[0].size_list
    draine_lam = temp_PAH_list[0].lam*u.micron


    #now read in the full PAH_list for all logU = 0 files (note, we'll
    #fill in any logU>>4 PAH spectra on a case by case basis later in
    #this module. these are relatively rare, and not worth the effort
    #to carry around all that information otherwise. the logic here is
    #that the majority of spectra from logU=[0,4] are nearly
    #identical, and also the vast majority of cells are logU<4.  So we
    #can treat the logU>=4 on an indvidiaul basis.)
    
    neutral_logU_iout_files = []
    ion_logU_iout_files = []
    for directory in draine_directories:
        neutral_logU_iout_files.append(glob.glob(directory+'/*iout_graD16*nb*_0.00')[0])
        ion_logU_iout_files.append(glob.glob(directory+'/*iout_graD16*ib*_0.00')[0])

    neutral_PAH_reference_objects = np.zeros([len(neutral_logU_iout_files),len(temp_PAH_list)],dtype=object)
    ion_PAH_reference_objects = np.zeros([len(ion_logU_iout_files),len(temp_PAH_list)],dtype=object)

    print("[pah/pah_source_create:] building the reference PAH list for neutrals")
    for counter,neutral_file in tqdm(enumerate(neutral_logU_iout_files)):
        neutral_PAH_reference_objects[counter,:] = np.asarray(read_draine_file(neutral_file))

    print("[pah/pah_source_create:] building the reference PAH list for ions")
    for counter,ion_file in tqdm(enumerate(ion_logU_iout_files)):
        ion_PAH_reference_objects[counter,:] = np.asarray(read_draine_file(ion_file))

    #third, on a cell-by-cell basis, interpolate the luminosity for
    #each grain size bin, and multiply by the number of grains in that
    #bin
    ncells = grid_of_sizes.shape[0]

    total_PAH_luminosity = np.zeros(len(temp_PAH_list[0].lam))
    
    #get the logU and beta_nnls for the local ISRF
    beta_nnls,logU = get_beta_nnls(draine_directories,grid_of_sizes,simulation_sizes,reg)


    #find the indices of the Draine sizes that best match those that are in the simulation
    Draine_simulation_idx_left_edge_array = []
    for size in simulation_sizes.to(u.cm).value:
        idx0 = find_nearest(draine_sizes,size)
        #if draine_sizes[idx0] > size: idx0 -=1
        
        #this is really the nearest point in the Draine sizes to the
        #simulation_sizes. 
        Draine_simulation_idx_left_edge_array.append(idx0)




    #june 13th, 2022: now at this point we want to loop through every
    #cell: in this loop, we want to multiply the
    #neutral_PAH_reference_spectra [*DEBUG note - we can later go back
    #and figure out how to figure out when its an ion and when its a
    #neutral] by the appropriate beta_nnls to get a pah_grid for every
    #single cell.  this pah_grid will be, like it is below, an
    #n_draine_sizes,N_draine_wavelengths SED for every draine size.
    #we then (as below) dot this into the simulation_sizes and we're
    #good to go!

    #june 13th, 2022 -- some things to watch out for:

    #a. time...this may take way too long. if so, we'll want to pool.map this. maybe without chunking (ask prerak)

    #b. we need to remember to then go back in after the fact, and for
    #any cells which have logU>>4, do those manually (i.e. create
    #reference spectra for those manually, and dot product those manually.
    

    #get the indices for where the Draine size bins match ours 
    size_arange = np.arange(len(simulation_sizes))
    draine_bins_idx = np.asarray(Draine_simulation_idx_left_edge_array)[size_arange]

    #pah_grid = np.array([x.lum for x in temp_PAH_list])


    #initialize the process pool and build the chunks
    t1 = datetime.now()
    nprocesses = np.min([cfg.par.n_processes,ncells]) #pool.map will barf in the corner case that we have less cells than cores
    p = Pool(processes = nprocesses)

    cell_list = np.arange(ncells)
    #DEBUG DEBUG DEBUG 
    cell_list = np.arange(1000)

    #chunking
    nchunks=nprocesses
    chunk_start_indices = []
    chunk_start_indices.append(0) #the start index is obviously 0
    #this should just be int(ncells/nchunks) but in case ncells < nchunks, we need to ensure that this is at least  1
    delta_chunk_indices = np.max([int(len(cell_list) / nchunks),1])
    print ('delta_chunk_indices = ',delta_chunk_indices)

    for n in range(1,nchunks):
        chunk_start_indices.append(chunk_start_indices[n-1]+delta_chunk_indices)

    list_of_chunks = []
    for n in range(nchunks):
        cells_list_chunk = cell_list[chunk_start_indices[n]:chunk_start_indices[n]+delta_chunk_indices]
        #if we're on the last chunk, we might not have the full list included, so need to make sure that we have that here
        if n == nchunks-1:
            cells_list_chunk = cell_list[chunk_start_indices[n]::]
        list_of_chunks.append(cells_list_chunk)



    print("Computing the PAH luminosities for every cell given its grain size distribution and logU. Entering Pool.map multiprocessing.")
    
    dum_numgrains = reg['particle_dust','numgrains'].value 
    '''
    answer = p.map(partial(compute_grid_PAH_luminosity,
                           beta_nnls = beta_nnls,
                           grid_of_sizes = grid_of_sizes.value,
                           numgrains = dum_numgrains,
                           draine_sizes = draine_sizes,
                           draine_lam = draine_lam.value,
                           neutral_PAH_reference_objects = neutral_PAH_reference_objects,
                           draine_bins_idx = draine_bins_idx),cell_list)
    '''
    
    answer = p.map(partial(compute_grid_PAH_luminosity,
                           beta_nnls = beta_nnls,
                           grid_of_sizes = grid_of_sizes.value,
                           numgrains = dum_numgrains,
                           draine_sizes = draine_sizes,
                           draine_lam = draine_lam.value,
                           neutral_PAH_reference_objects = neutral_PAH_reference_objects,
                           draine_bins_idx = draine_bins_idx),[arg for arg in list_of_chunks])
    
    t2 = datetime.now()
    print ('Execution time for PAH dot producting [is that a word?] across the grid = '+str(t2-t1))
 



   #z = zip(cell_list,beta_nnls,grid_of_sizes.value,dum_numgrains,draine_sizes,draine_lam.value,neutral_PAH_reference_objects,draine_bins_idx)
    #answer = p.starmap(compute_grid_PAH_luminosity,z)
    #p.close()
    #p.terminate()
    #p.join()

    #what i need back out is something like this with these dimensions
    #grid_PAH_luminosity = np.zeros([ncells,len(draine_lam)])
    #particle_PAH_luminosity = np.zeros([len(numgrains),len(draine_lam)])

    #grid_PAH_luminosity,particle_PAH_luminosity = compute_grid_PAH_luminosity(cell_list,beta_nnls,draine_sizes,draine_lam,neutral_PAH_reference_objects,grid_of_sizes,reg,draine_bins_idx)
    pdb.set_trace()

    grid_PAH_luminosity[np.isnan(grid_PAH_luminosity)] = 0
    particle_PAH_luminosity[np.isnan(particle_PAH_luminosity)] = 0

    nu = (constants.c/draine_lam).to(u.Hz)
    #the units here are Lsun/Hz - this is to be consistent with our
    #stellar fnu addition later. The SEDs of individiual sources all
    #end up getting renormalized by the luminosity, so the exact units
    #don't matter as long as they're consistent across all the sources
    #(and types of sources) being added to the grid.
    fnu = np.divide((particle_PAH_luminosity*u.erg/u.s).to(u.Lsun).value,nu.to(u.Hz).value)

    #Because the Draine templates include re-emission, but we want to
    #add the PAHs as sources only, we restrict to the PAH range.
    nu_reverse = nu[::-1]

    nu_pah2 = (constants.c/(3*u.micron)).to(u.Hz) #start of the pah range
    nu_pah1 = (constants.c/(20.*u.micron)).to(u.Hz) #end of pah range
    wpah_nu_reverse = np.where( (nu_reverse.value < nu_pah2.value) & (nu_reverse.value > nu_pah1.value))[0]





    for i in range(particle_PAH_luminosity.shape[0]): #np.arange(2500)
        #lum = np.trapz(draine_lam[wpah_lam].cgs.value,flam[i,wpah_lam]).value
        fnu_reverse = fnu[i,:][::-1]
        #if np.where(fnu_reverse == 0)[0] > 0:
        #    fnu_reverse[fnu_reverse ==0 ] = np.min(fnu_reverse[fnu_reverse > 0])

        lum = (np.absolute(np.trapz(nu_reverse[wpah_nu_reverse].cgs.value,fnu_reverse[wpah_nu_reverse])).item()*u.Lsun).to(u.erg/u.s).value
        
        if lum <= LUM_FLOOR: lum = LUM_FLOOR #just a jamky variable
                                            #defined at the top of
                                            #this function to define a
                                            #lowest luminosity so that
                                            #we don't add PAH cells
                                            #with 0 luminosity

        print(lum)
        #reversing arrays to make nu increasing, and therefore correct for hyperion addition
        #m.add_point_source(luminosity = lum,spectrum=(nu_reverse[wpah_nu_reverse].value,fnu_reverse[wpah_nu_reverse]), position = reg['particle_dust','coordinates'][i,:].in_units('cm').value-boost)


        m.add_point_source(luminosity=lum,spectrum=(nu_reverse[wpah_nu_reverse].value,fnu_reverse[wpah_nu_reverse]),position=reg['particle_dust','coordinates'][i,:].in_units('cm').value-boost)


    if cfg.par.draine21_pah_grid_write: #else, the try/except in analytics.py will get caught and will just write a single -1 to the output npz file
        reg.parameters['grid_PAH_luminosity'] = grid_PAH_luminosity
    reg.parameters['PAH_lam'] = draine_lam.value

    total_PAH_luminosity =np.sum(grid_PAH_luminosity,axis=0)
    reg.parameters['total_PAH_luminosity'] = total_PAH_luminosity
    
    grid_PAH_L_lam = grid_PAH_luminosity/draine_lam.value
    integrated_grid_PAH_luminosity = np.trapz((grid_PAH_luminosity/draine_lam.value),draine_lam.value,axis=1)
    reg.parameters['integrated_grid_PAH_luminosity'] = integrated_grid_PAH_luminosity
    
    #save some information for dumping into analytics
    reg.parameters['q_pah'] = q_pah
    reg.parameters['particle_mass_weighted_gsd'] = particle_mass_weighted_gsd
    reg.parameters['grid_mass_weighted_gsd'] = grid_mass_weighted_gsd
    reg.parameters['simulation_sizes'] = simulation_sizes


#    for i_cell in tqdm(range(ncells)):
        
        
        #in principle this doesn't need to be done inside the loop for
        #a constant radiation field. However, for a radiation field
        #that varies cell by cell, then PAH_list will change as we
        #have different files that we read in, so we may as well keep it here for now.

 #       pah_grid = np.array([x.lum for x in PAH_list])
 #       idx = np.asarray(Draine_simulation_idx_left_edge_array)[size_arange]
        
        #set the PAH luminosity of the cell to be the dot product of
        #the Draine luminosities (i.e., pah_grid[idx,:] which has
        #dimensions (simulation_sizes,wavelengths)) with the actual
        #grain size distribution in that cell (i.e.,
        #grid_of_sizes[i_cell,:]). note, we take the transpose of
        #grid_of_sizes to get the dimensions to match up correctly for the dot product

  
  #      grid_PAH_luminosity[i_cell,:] = np.dot(pah_grid[idx,:].T,grid_of_sizes[i_cell,:].T)

  


    #import matplotlib.pyplot as plt
    #fig = plt.figure()
    #ax = fig.add_subplot(111)
    #ax.loglog(PAH_list[0].lam,total_PAH_luminosity[:]/PAH_list[0].lam)
    #ax.set_ylim([1e31,1e45])
    #ax.set_xlim([1,1000])
    #ax.set_xlabel(r'$\lambda (\mu $m)')
    #ax.set_ylabel(r'$L_\lambda$ (erg/s/$\mu$m)')
    #fig.savefig('/home/desika.narayanan/PAH_sed.png',dpi=300)
    
